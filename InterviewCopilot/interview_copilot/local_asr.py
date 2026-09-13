"""Crash-isolated, persistent local ASR; all audio IPC remains in memory.

Only the child imports Whisper and its native dependencies. Closing a worker
interrupts loading/inference even when a native call cannot cooperate.
"""
from __future__ import annotations

import base64
from array import array
from io import BytesIO
import json
import math
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import wave

if __package__:
    from .runtime import child_command
else:
    from runtime import child_command


class LocalASRError(RuntimeError):
    pass


class LocalASRCancelled(LocalASRError):
    pass


def recognition_options(quality="balanced") -> dict:
    if quality not in {"fast", "balanced", "accurate"}:
        raise LocalASRError("语音识别精度请选择快速、均衡或准确。")
    return {"beam_size": {"fast": 1, "balanced": 3, "accurate": 5}[quality],
        "best_of": 1, "temperature": 0, "vad_filter": True,
        # Keep more of quiet syllables around speech edges. The outer detector
        # already waits for the user-selected endpoint; this adds no live wait.
        "vad_parameters": {"threshold": 0.35, "min_silence_duration_ms": 400,
                           "speech_pad_ms": 400},
        "condition_on_previous_text": False, "no_speech_threshold": 0.6,
        "compression_ratio_threshold": 2.4}


def prepare_local_wave(wav_bytes: bytes):
    """Select a whole channel before downmix without amplifying background noise.

    Averaging opposite-phase stereo can turn speech into silence. Selecting one
    channel for the full bounded phrase avoids cancellation and rapid switching.
    This preserves the source volume; it does not denoise or restore clipped audio.
    """
    with wave.open(BytesIO(wav_bytes), "rb") as source:
        channels, rate = source.getnchannels(), source.getframerate()
        if source.getsampwidth() != 2 or channels < 1:
            raise LocalASRError("本地识别需要 PCM16 音频。")
        raw = array("h", source.readframes(source.getnframes()))
    if sys.byteorder != "little":
        raw.byteswap()
    if not raw:
        return wav_bytes, {"has_signal": False, "input_rms": 0.0, "gain": 1.0}
    candidates = [raw[index::channels] for index in range(channels)]
    means = [sum(values) / len(values) for values in candidates]
    powers = [max(0.0, sum(v * v for v in values) / len(values) - mean * mean)
              for values, mean in zip(candidates, means)]
    index = max(range(channels), key=lambda item: powers[item])
    mean = round(means[index])
    values = [max(-32768, min(32767, value - mean)) for value in candidates[index]]
    rms = math.sqrt(powers[index]) / 32768
    peak = max(abs(value) for value in values) / 32768
    gain = 1.0
    normalized = array("h", (max(-32768, min(32767, round(value * gain))) for value in values))
    if sys.byteorder != "little":
        normalized.byteswap()
    buffer = BytesIO()
    with wave.open(buffer, "wb") as destination:
        destination.setnchannels(1)
        destination.setsampwidth(2)
        destination.setframerate(rate)
        destination.writeframes(normalized.tobytes())
    return buffer.getvalue(), {"has_signal": rms >= 0.0008 and peak >= 0.003,
        "input_rms": round(rms, 6), "input_peak": round(peak, 6),
        "gain": round(gain, 3), "source_channel": index + 1}


class LocalASRWorker:
    def __init__(self, name: str, model_dir: Path, *, startup_timeout=180,
                 inference_timeout=45, _command=None):
        self.name = name
        self.model_dir = Path(model_dir)
        self.startup_timeout = startup_timeout
        self.inference_timeout = inference_timeout
        self._command = _command or child_command("--asr-worker")
        self._state_lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = threading.Event()
        self._process = None
        self._responses = queue.Queue(maxsize=8)
        self._ready = False
        self.last_metadata = {}

    @property
    def ready(self):
        with self._state_lock:
            return (self._ready and not self._closed.is_set()
                    and self._process is not None and self._process.poll() is None)

    def _check_cancel(self, cancel):
        if self._closed.is_set() or (cancel is not None and cancel.is_set()):
            raise LocalASRCancelled("本地语音识别已取消。")

    def _acquire(self, cancel):
        while True:
            self._check_cancel(cancel)
            if self._request_lock.acquire(timeout=0.05):
                try:
                    self._check_cancel(cancel)
                except BaseException:
                    self._request_lock.release()
                    raise
                return

    def _start(self, cancel):
        with self._state_lock:
            self._check_cancel(cancel)
            if self._process is not None:
                return
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            try:
                self._process = subprocess.Popen(self._command, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                    creationflags=flags)
            except OSError as exc:
                raise LocalASRError("无法启动本地语音识别子进程，请检查 Python 环境后重新启动工具。") from exc
            process = self._process

        def read_responses():
            try:
                while line := process.stdout.readline():
                    try:
                        value = json.loads(line)
                    except (ValueError, UnicodeError):
                        value = {"ok": False, "error": "protocol"}
                    try:
                        self._responses.put_nowait(value)
                    except queue.Full:
                        break
            except (OSError, ValueError):
                pass
            finally:
                try:
                    self._responses.put_nowait(None)
                except queue.Full:
                    pass
                process.stdout.close()

        threading.Thread(target=read_responses, name="local-asr-responses", daemon=True).start()

    def _exit_error(self):
        process = self._process
        code = process.poll() if process is not None else None
        if process is not None and code is None:
            try:
                code = process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
        detail = f"（代码 0x{code & 0xFFFFFFFF:08X}）" if code is not None else ""
        return LocalASRError("本地语音识别子进程已退出" + detail
            + "。请重新安装兼容的语音组件后重试，或切换云端识别；主程序仍可继续使用。")

    def _exchange(self, payload, timeout, cancel):
        self._check_cancel(cancel)
        process = self._process
        if process is None or process.poll() is not None:
            error = self._exit_error()
            self.close()
            raise error
        # A pipe write can itself block if native inference hangs. Keep it off
        # the controlling thread so the same deadline and stop() can kill it.
        encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        write_failed = threading.Event()
        def send():
            try:
                remaining = memoryview(encoded)
                while remaining:
                    written = process.stdin.write(remaining)
                    if not written:
                        raise OSError("ASR pipe closed")
                    remaining = remaining[written:]
                process.stdin.flush()
            except (OSError, ValueError):
                write_failed.set()
        threading.Thread(target=send, name="local-asr-request", daemon=True).start()
        deadline = time.monotonic() + timeout
        while True:
            self._check_cancel(cancel)
            if time.monotonic() >= deadline:
                self.close()
                operation = "加载/预热" if payload["op"] == "init" else "识别"
                raise LocalASRError("本地语音模型" + operation
                    + "超时，已结束子进程。请检查模型下载网络、改用 tiny 模型或切换云端识别。")
            try:
                response = self._responses.get(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                if write_failed.is_set() or process.poll() is not None:
                    error = self._exit_error()
                    self.close()
                    raise error
                continue
            self._check_cancel(cancel)
            if response is None:
                error = self._exit_error()
                self.close()
                raise error
            if not isinstance(response, dict) or response.get("ok") is not True:
                code = response.get("error") if isinstance(response, dict) else "protocol"
                self.close()
                messages = {
                    "runtime": "本地语音需要新版 Microsoft Visual C++ x64 运行组件。请安装微软最新版运行库后重试，或切换云端识别。",
                    "dependency": "本地语音模型组件未就绪，请运行安装脚本安装兼容的 requirements-audio.txt。",
                    "load": "本地语音模型加载或预热失败。首次需下载模型，请检查网络、磁盘空间及语音组件，或切换云端识别。",
                    "transcribe": "本地语音识别失败，请重新安装兼容的语音组件、改用 tiny 模型或切换云端识别。",
                }
                raise LocalASRError(messages.get(code, "本地语音识别子进程通信失败，请重新启动监听。"))
            return response

    def initialize(self, cancel=None):
        acquired = False
        try:
            self._acquire(cancel)
            acquired = True
            if self.ready:
                return self
            self._start(cancel)
            self._exchange({"op": "init", "name": self.name, "model_dir": str(self.model_dir)},
                           self.startup_timeout, cancel)
            with self._state_lock:
                self._check_cancel(cancel)
                self._ready = True
            return self
        except LocalASRCancelled:
            self.close()
            raise
        finally:
            if acquired:
                self._request_lock.release()

    def transcribe(self, wav: bytes, *, language=None, quality="balanced", cancel=None):
        recognition_options(quality)  # Validate before starting native work.
        self.initialize(cancel)
        acquired = False
        try:
            self._acquire(cancel)
            acquired = True
            result = self._exchange({"op": "transcribe", "language": language,
                "quality": quality,
                "wav": base64.b64encode(wav).decode("ascii")}, self.inference_timeout, cancel)
            self._check_cancel(cancel)
            if not isinstance(result.get("text"), str):
                self.close()
                raise LocalASRError("本地语音识别返回了无效结果，请重新启动监听。")
            self.last_metadata = result.get("metadata", {}) if isinstance(result.get("metadata", {}), dict) else {}
            return result["text"].strip()
        except LocalASRCancelled:
            self.close()
            raise
        finally:
            if acquired:
                self._request_lock.release()

    def close(self):
        with self._close_lock:
            with self._state_lock:
                self._closed.set()
                self._ready = False
                process = self._process
            if process is None:
                return
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=0.75)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                        process.wait(timeout=0.75)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                except OSError:
                    pass
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass
