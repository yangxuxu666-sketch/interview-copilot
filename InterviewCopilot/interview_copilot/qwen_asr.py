"""Cancellable, per-session wrapper around the official DashScope realtime SDK.

Audio is continuous 16 kHz mono, signed little-endian PCM16. Callbacks run on
the SDK receive thread and must return promptly. No audio is saved to disk.
"""
from __future__ import annotations

import base64
from collections import deque
import logging
import queue
import threading
import time
from typing import Callable


QWEN_ASR_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
QWEN_ASR_MODEL = "qwen3-asr-flash-realtime"


class QwenASRError(RuntimeError):
    """A safe, user-facing ASR failure, without provider response bodies."""


class QwenASRCancelled(QwenASRError):
    """The caller stopped this session."""


class _PrivateRealtimeLogs(logging.Filter):
    def filter(self, record):
        # This SDK module logs incoming transcripts and raw remote errors.
        # Keep other DashScope logging intact, even when SDK debug is enabled.
        path = record.pathname.replace("\\", "/")
        return not path.endswith("/audio/qwen_omni/omni_realtime.py")


def _load_sdk():
    from dashscope.audio.qwen_omni import (
        MultiModality, OmniRealtimeCallback, OmniRealtimeConversation,
    )
    from dashscope.audio.qwen_omni.omni_realtime import TranscriptionParams
    from dashscope.common.logging import logger

    if not any(isinstance(item, _PrivateRealtimeLogs) for item in logger.filters):
        logger.addFilter(_PrivateRealtimeLogs())
    return OmniRealtimeConversation, OmniRealtimeCallback, MultiModality, TranscriptionParams


def _safe_failure(error=None, code=None):
    """Classify structured codes only; never interpolate remote/exception text."""
    if code is None:
        code = getattr(error, "status_code", None)
    value = str(code or "").lower().replace("_", "").replace("-", "")
    if value in {"401", "403", "4001", "invalidapikey", "invalidapi-key",
                 "unauthorized", "authenticationerror", "authenticationfailed",
                 "invalidcredential", "accessdenied"}:
        return "阿里云语音识别鉴权失败，请检查百炼北京地域 API Key 和模型权限。"
    if value in {"429", "throttling", "ratelimitexceeded", "limitrequests",
                 "insufficientquota", "arrearage", "quotaexceeded"}:
        return "阿里云语音识别额度不足或请求过于频繁，请检查百炼余额与限额。"
    if isinstance(error, TimeoutError):
        return "阿里云实时语音连接超时，请检查网络后重新开始监听。"
    return "阿里云实时语音连接失败或已中断，请检查网络、API Key 和服务状态后重试。"


class QwenASRStream:
    """One realtime session. Closed sessions cannot be reused.

    ``close`` cancels without flushing more answers. ``finish`` explicitly
    requests remaining transcripts. Do not hold caller locks while closing:
    an already-running callback is allowed to finish before close returns.
    """

    def __init__(self, api_key: str, *, language: str = "auto",
                 silence_ms: int = 700, on_final: Callable[[str], None],
                 on_partial: Callable[[str], None] | None = None,
                 connect_timeout: float = 8.0):
        if not isinstance(api_key, str) or not api_key.strip():
            raise QwenASRError("请先填写百炼北京地域 API Key，再开始实时语音识别。")
        if (not api_key.isascii() or any(ch.isspace() for ch in api_key.strip())
                or len(api_key) > 512):
            raise QwenASRError("百炼 API Key 格式不正确，请重新粘贴。")
        if language not in {"zh", "en", "auto"}:
            raise QwenASRError("实时识别语言须为中文、英文或自动。")
        if not isinstance(silence_ms, int) or not 200 <= silence_ms <= 6000:
            raise QwenASRError("语音停顿时间应为 200 至 6000 毫秒。")
        if not 0 < connect_timeout <= 30:
            raise QwenASRError("实时识别连接超时设置不正确。")
        self._api_key = api_key.strip()
        self.language = language
        self.silence_ms = silence_ms
        self.connect_timeout = connect_timeout
        self._on_final = on_final
        self._on_partial = on_partial
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._configured = threading.Event()
        self._finished = threading.Event()
        self._cancel = None
        self._error: str | None = None
        self._state = "new"
        self._conversation = None
        self._operations = queue.Queue(maxsize=2)
        self._worker = None
        self._completed_items = deque(maxlen=256)

    def connect(self, cancel=None):
        self._cancel = cancel
        self.check_error()
        with self._lock:
            if self._state != "new":
                raise QwenASRError("此实时识别会话已启动，请创建新会话后重试。")
            self._state = "connecting"
        deadline = time.monotonic() + self.connect_timeout
        try:
            Conversation, Callback, Modality, Params = _load_sdk()
        except Exception:
            self.close()
            raise QwenASRError("实时语音组件未安装完整，请重新运行安装程序。") from None

        owner = self

        class Events(Callback):
            def on_event(self, message):
                owner._event(message)

            def on_close(self, close_status_code, close_msg):
                if not owner._closed.is_set() and not owner._finished.is_set():
                    owner._record_error(_safe_failure(code=close_status_code))

        class PrivateConversation(Conversation):
            def _on_error(self, ws, error):
                # Official SDK logs raw errors but never forwards them. Surface
                # a curated error and suppress potentially reflected secrets.
                owner._record_error(_safe_failure(error))

        try:
            conversation = PrivateConversation(
                model=QWEN_ASR_MODEL, callback=Events(), url=QWEN_ASR_URL,
                api_key=self._api_key,
            )
            with self._lock:
                self.check_error()
                self._conversation = conversation
                self._worker = threading.Thread(target=self._io_loop,
                                                name="qwen-asr-io", daemon=True)
                self._worker.start()

            def configure():
                conversation.connect()
                if self._closed.is_set():
                    return
                conversation.update_session(
                    output_modalities=[Modality.TEXT],
                    enable_input_audio_transcription=True,
                    enable_turn_detection=True,
                    turn_detection_type="server_vad",
                    turn_detection_threshold=0.0,
                    turn_detection_silence_duration_ms=self.silence_ms,
                    transcription_params=Params(
                        language=None if self.language == "auto" else self.language,
                        sample_rate=16000, input_audio_format="pcm",
                    ),
                )

            self._call(configure, deadline)
            self._wait(self._configured, deadline, "阿里云实时语音初始化超时，请检查网络后重试。")
            with self._lock:
                self.check_error()
                self._state = "ready"
        except QwenASRError:
            self.close()
            raise
        except Exception as error:
            self.close()
            raise QwenASRError(_safe_failure(error)) from None

    def send_audio(self, pcm: bytes):
        self.check_error()
        if not isinstance(pcm, bytes) or len(pcm) % 2:
            raise QwenASRError("实时语音数据应为 16 kHz 单声道 PCM16。")
        if not pcm:
            return
        with self._lock:
            if self._state != "ready":
                raise QwenASRError("实时语音连接尚未就绪或已结束。")
        # Bound each request even if a caller supplies more than the usual
        # 100 ms packet. One worker is reused; no thread per audio packet.
        for offset in range(0, len(pcm), 3200):
            encoded = base64.b64encode(pcm[offset:offset + 3200]).decode("ascii")
            self._call(lambda data=encoded: self._conversation.append_audio(data),
                       time.monotonic() + 5.0)

    def check_error(self):
        if self._cancel is not None and self._cancel.is_set():
            self.close()
            raise QwenASRCancelled("实时识别已停止。")
        with self._lock:
            if self._error:
                raise QwenASRError(self._error)
            if self._closed.is_set():
                raise QwenASRCancelled("实时识别已停止。")

    def finish(self, timeout: float = 3.0):
        """Flush the last utterance; only use when final callbacks are wanted."""
        self.check_error()
        if not 0 < timeout <= 30:
            raise QwenASRError("实时识别结束等待时间不正确。")
        with self._lock:
            if self._state != "ready":
                raise QwenASRError("实时语音连接尚未就绪或已结束。")
            self._state = "finishing"
        deadline = time.monotonic() + timeout
        try:
            self._call(self._conversation.end_session_async, deadline)
            self._wait(self._finished, deadline, "等待最后一句识别结果超时，请重新开始监听。")
        finally:
            self.close()

    def close(self):
        """Cancel immediately; discard queued audio and all future callbacks."""
        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()
            self._state = "closed"
            self._api_key = ""
        while True:
            try:
                self._operations.get_nowait()
            except queue.Empty:
                break
        try:
            self._operations.put_nowait(None)
        except queue.Full:
            pass
        self._abort_socket()
        if self._conversation is not None:
            # SDK's graceful WebSocket close can wait 3 seconds. It must never
            # hold up the UI or the capture thread on user Stop.
            threading.Thread(target=self._close_sdk, name="qwen-asr-close",
                             daemon=True).start()

    def _abort_socket(self):
        ws = getattr(self._conversation, "ws", None)
        if ws is not None:
            ws.keep_running = False
            sock = getattr(ws, "sock", None)
            if sock is not None:
                try:
                    sock.abort()
                except Exception:
                    pass

    def _close_sdk(self):
        try:
            if getattr(self._conversation, "ws", None) is not None:
                self._conversation.close()
        except Exception:
            pass

    def _record_error(self, message):
        with self._lock:
            if not self._closed.is_set() and self._error is None:
                self._error = message

    def _event(self, message):
        if not isinstance(message, dict):
            return
        with self._lock:
            if self._closed.is_set() or self._error is not None:
                return
            kind = message.get("type")
            if kind == "session.updated":
                self._configured.set()
            elif kind == "session.finished":
                self._finished.set()
            elif kind in {"error", "conversation.item.input_audio_transcription.failed"}:
                error = message.get("error")
                self._record_error(_safe_failure(code=error.get("code")
                                   if isinstance(error, dict) else None))
            elif kind == "conversation.item.input_audio_transcription.completed":
                value = message.get("transcript")
                item_id = message.get("item_id")
                if isinstance(item_id, str) and item_id in self._completed_items:
                    return
                if isinstance(value, str) and value.strip():
                    if isinstance(item_id, str):
                        self._completed_items.append(item_id)
                    self._deliver(self._on_final, value.strip())
            elif kind == "conversation.item.input_audio_transcription.text":
                value = message.get("text", "")
                stash = message.get("stash", "")
                if isinstance(value, str) and isinstance(stash, str):
                    self._deliver(self._on_partial, (value + stash).strip())

    def _deliver(self, callback, text):
        if callback is not None and text:
            try:
                callback(text)
            except Exception:
                self._record_error("处理实时语音识别结果失败，请重新开始监听。")

    def _io_loop(self):
        try:
            while not self._closed.is_set():
                operation = self._operations.get()
                if operation is None or self._closed.is_set():
                    return
                function, completed = operation
                try:
                    function()
                except Exception as error:
                    self._record_error(_safe_failure(error))
                finally:
                    completed.set()
        finally:
            # connect() may have created its socket after cancellation. Close
            # again here so that a late successful connection cannot escape.
            if self._closed.is_set():
                self._abort_socket()
                self._close_sdk()

    def _call(self, function, deadline):
        self.check_error()
        completed = threading.Event()
        try:
            self._operations.put_nowait((function, completed))
        except queue.Full:
            self._record_error("实时语音发送积压，请检查网络后重新开始监听。")
            self.close()
            self.check_error()
        self._wait(completed, deadline, "阿里云实时语音发送或连接超时，请检查网络后重试。")

    def _wait(self, event, deadline, timeout_message):
        while True:
            self.check_error()
            if event.is_set():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._record_error(timeout_message)
                self.close()
                raise QwenASRError(timeout_message)
            event.wait(min(remaining, 0.05))
