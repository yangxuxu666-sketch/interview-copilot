"""Realtime adapter tests: synthetic PCM and a fake SDK; no network or keys."""
import base64
import json
import logging
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import patch

from interview_copilot.qwen_asr import (
    QWEN_ASR_MODEL, QWEN_ASR_URL, QwenASRCancelled, QwenASRError,
    QwenASRStream, _PrivateRealtimeLogs, _load_sdk,
)


class FakeCallback:
    pass


class FakeConversation:
    instances = []
    connect_block = False
    send_block = False
    acknowledge = True
    finish_ack = True
    create_socket_late = False

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.callback = kwargs["callback"]
        self.sent = []
        self.config = None
        self.started = threading.Event()
        self.sending = threading.Event()
        self.released = threading.Event()
        self.did_close = threading.Event()
        self.ws = None if self.create_socket_late else self.new_socket()
        self.finish_count = 0
        self.instances.append(self)

    def new_socket(self):
        return SimpleNamespace(sock=SimpleNamespace(abort=self.released.set), keep_running=True)

    def connect(self):
        self.started.set()
        if self.connect_block:
            self.released.wait(2)
        if self.create_socket_late:
            self.ws = self.new_socket()

    def update_session(self, **kwargs):
        self.config = kwargs
        if self.acknowledge:
            self.callback.on_event({"type": "session.updated"})

    def append_audio(self, audio):
        self.sending.set()
        if self.send_block:
            self.released.wait(2)
        self.sent.append(audio)

    def end_session_async(self):
        self.finish_count += 1
        self.final("最后一句", "final-flush")
        if self.finish_ack:
            self.callback.on_event({"type": "session.finished"})

    def close(self):
        self.released.set()
        self.did_close.set()
        self.callback.on_close(1000, "ignored remote message")

    def final(self, text, item_id=None):
        self.callback.on_event({"type": "conversation.item.input_audio_transcription.completed",
                                "transcript": text, "item_id": item_id})


class QwenStreamTests(unittest.TestCase):
    def setUp(self):
        FakeConversation.instances = []
        for name, value in {"connect_block": False, "send_block": False,
                            "acknowledge": True, "finish_ack": True,
                            "create_socket_late": False}.items():
            setattr(FakeConversation, name, value)
        self.loader = patch("interview_copilot.qwen_asr._load_sdk", return_value=(
            FakeConversation, FakeCallback, SimpleNamespace(TEXT="text"), SimpleNamespace))
        self.loader.start()
        self.streams = []

    def tearDown(self):
        for stream in self.streams:
            stream.close()
        for conversation in FakeConversation.instances:
            conversation.released.set()
        for stream in self.streams:
            if stream._worker:
                stream._worker.join(1)
        self.loader.stop()

    def stream(self, **kwargs):
        kwargs.setdefault("on_final", lambda text: None)
        stream = QwenASRStream("sk-test-private", **kwargs)
        self.streams.append(stream)
        return stream

    def connected(self, **kwargs):
        stream = self.stream(**kwargs)
        stream.connect()
        return stream, FakeConversation.instances[-1]

    def run_thread(self, fn):
        errors = []

        def run():
            try:
                fn()
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        return thread, errors

    def test_fixed_endpoint_and_model_use_explicit_instance_key(self):
        stream, conversation = self.connected()
        self.assertEqual(conversation.kwargs["api_key"], "sk-test-private")
        self.assertEqual(conversation.kwargs["url"], QWEN_ASR_URL)
        self.assertEqual(conversation.kwargs["model"], QWEN_ASR_MODEL)
        self.assertNotIn("?", conversation.kwargs["url"])
        self.assertEqual(conversation.config["output_modalities"], ["text"])
        self.assertTrue(conversation.config["enable_turn_detection"])
        self.assertEqual(conversation.config["turn_detection_type"], "server_vad")
        self.assertEqual(conversation.config["turn_detection_silence_duration_ms"], 700)
        self.assertEqual(conversation.config["turn_detection_threshold"], 0.0)
        params = conversation.config["transcription_params"]
        self.assertIsNone(params.language)
        self.assertEqual(params.sample_rate, 16000)
        self.assertEqual(params.input_audio_format, "pcm")

    def test_language_can_be_explicit_chinese_or_english(self):
        for language in ("zh", "en"):
            with self.subTest(language=language):
                stream, conversation = self.connected(language=language, silence_ms=800)
                self.assertEqual(conversation.config["transcription_params"].language, language)

    def test_continuous_audio_is_sent_without_local_segmentation(self):
        stream, conversation = self.connected()
        data = b"\x00\x01" * 5000
        stream.send_audio(data)
        self.assertEqual([len(base64.b64decode(packet)) for packet in conversation.sent],
                         [3200, 3200, 3200, 400])
        self.assertEqual(b"".join(base64.b64decode(packet) for packet in conversation.sent), data)

    def test_final_partial_and_duplicate_item_handling(self):
        finals, partials = [], []
        stream, conversation = self.connected(on_final=finals.append, on_partial=partials.append)
        conversation.callback.on_event({"type": "conversation.item.input_audio_transcription.text",
                                        "text": "项目", "stash": "经验"})
        conversation.final(" 项目经验。 ", "one")
        conversation.final("项目经验。", "one")
        conversation.final("项目经验。", "two")
        conversation.final("", "empty")
        self.assertEqual(finals, ["项目经验。", "项目经验。"])
        self.assertEqual(partials, ["项目经验"])

    def test_stop_never_flushes_or_delivers_late_callbacks(self):
        finals, partials = [], []
        stream, conversation = self.connected(on_final=finals.append, on_partial=partials.append)
        stream.close()
        conversation.final("迟来的结果", "late")
        conversation.callback.on_event({"type": "conversation.item.input_audio_transcription.text",
                                        "text": "迟来的片段"})
        conversation.callback.on_event({"type": "error", "error": {"code": "401"}})
        self.assertEqual(finals, [])
        self.assertEqual(partials, [])
        self.assertEqual(conversation.finish_count, 0)
        with self.assertRaises(QwenASRCancelled):
            stream.check_error()

    def test_finish_flushes_final_then_closes(self):
        finals = []
        stream, conversation = self.connected(on_final=finals.append)
        stream.finish()
        self.assertEqual(finals, ["最后一句"])
        self.assertEqual(conversation.finish_count, 1)
        self.assertTrue(stream._closed.is_set())

    def test_finish_timeout_is_bounded(self):
        FakeConversation.finish_ack = False
        stream, conversation = self.connected()
        started = time.monotonic()
        with self.assertRaisesRegex(QwenASRError, "最后一句.*超时"):
            stream.finish(timeout=0.06)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(stream._closed.is_set())

    def test_connect_requires_session_updated_acknowledgment(self):
        FakeConversation.acknowledge = False
        stream = self.stream(connect_timeout=0.06)
        with self.assertRaisesRegex(QwenASRError, "初始化超时"):
            stream.connect()
        self.assertTrue(stream._closed.is_set())

    def test_connect_event_cancellation_returns_promptly(self):
        FakeConversation.connect_block = True
        cancel = threading.Event()
        stream = self.stream()
        thread, errors = self.run_thread(lambda: stream.connect(cancel=cancel))
        while not FakeConversation.instances:
            time.sleep(0.002)
        conversation = FakeConversation.instances[-1]
        self.assertTrue(conversation.started.wait(0.5))
        cancel.set()
        thread.join(0.4)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], QwenASRCancelled)
        self.assertTrue(conversation.did_close.wait(0.5))
        self.assertIsNone(conversation.config)

    def test_socket_created_after_cancel_is_closed(self):
        FakeConversation.connect_block = True
        FakeConversation.create_socket_late = True
        stream = self.stream()
        thread, errors = self.run_thread(stream.connect)
        while not FakeConversation.instances:
            time.sleep(0.002)
        conversation = FakeConversation.instances[-1]
        self.assertTrue(conversation.started.wait(0.5))
        stream.close()
        thread.join(0.4)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], QwenASRCancelled)
        conversation.released.set()
        self.assertTrue(conversation.did_close.wait(0.5))
        self.assertIsNone(conversation.config)

    def test_close_interrupts_a_blocked_audio_send(self):
        FakeConversation.send_block = True
        stream, conversation = self.connected()
        thread, errors = self.run_thread(lambda: stream.send_audio(b"\0\0" * 1600))
        self.assertTrue(conversation.sending.wait(0.5))
        started = time.monotonic()
        stream.close()
        thread.join(0.4)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], QwenASRCancelled)

    def test_http_auth_error_is_curated_without_response_body(self):
        stream, conversation = self.connected()
        error = RuntimeError("Authorization Bearer sk-test-private; remote-body")
        error.status_code = 401
        conversation._on_error(None, error)
        with self.assertRaises(QwenASRError) as raised:
            stream.check_error()
        self.assertIn("鉴权失败", str(raised.exception))
        self.assertNotIn("sk-test-private", str(raised.exception))
        self.assertNotIn("remote-body", str(raised.exception))

    def test_provider_error_codes_and_transcription_failure_are_sanitized(self):
        for kind, code, expected in [
            ("error", "InvalidApiKey", "鉴权"),
            ("error", "429", "额度"),
            ("conversation.item.input_audio_transcription.failed", "Unknown", "连接失败"),
        ]:
            with self.subTest(kind=kind, code=code):
                stream, conversation = self.connected()
                conversation.callback.on_event({"type": kind, "error": {
                    "code": code, "message": "sk-test-private secret details"}})
                with self.assertRaises(QwenASRError) as raised:
                    stream.check_error()
                self.assertIn(expected, str(raised.exception))
                self.assertNotIn("secret", str(raised.exception))
                self.assertNotIn("sk-test-private", str(raised.exception))

    def test_unexpected_disconnect_surfaces_even_without_more_audio(self):
        stream, conversation = self.connected()
        conversation.callback.on_close(1006, "sk-test-private")
        with self.assertRaisesRegex(QwenASRError, "中断"):
            stream.check_error()

    def test_callback_exception_does_not_escape_or_leak(self):
        def bad_callback(text):
            raise RuntimeError("sk-test-private")
        stream, conversation = self.connected(on_final=bad_callback)
        conversation.final("test")
        with self.assertRaisesRegex(QwenASRError, "处理实时语音") as raised:
            stream.check_error()
        self.assertNotIn("sk-test-private", str(raised.exception))

    def test_missing_sdk_has_clear_error(self):
        stream = self.stream()
        with patch("interview_copilot.qwen_asr._load_sdk", side_effect=ImportError("secret")):
            with self.assertRaisesRegex(QwenASRError, "组件未安装完整"):
                stream.connect()
        self.assertTrue(stream._closed.is_set())

    def test_invalid_key_language_or_pcm_never_reaches_sdk(self):
        for key in ("", "  ", "sk-秘密", "sk-a\nb", "a" * 513):
            with self.subTest(key_length=len(key)):
                with self.assertRaises(QwenASRError):
                    QwenASRStream(key, on_final=lambda text: None)
        with self.assertRaises(QwenASRError):
            self.stream(language="ja")
        stream, conversation = self.connected()
        with self.assertRaises(QwenASRError):
            stream.send_audio(b"\0")
        self.assertEqual(conversation.sent, [])


class OfficialSDKSchemaTests(unittest.TestCase):
    def test_installed_sdk_serializes_exact_asr_session_schema(self):
        try:
            Conversation, Callback, Modality, Params = _load_sdk()
        except ImportError:
            self.skipTest("Optional DashScope SDK not installed")
        packets = []
        conversation = Conversation(model=QWEN_ASR_MODEL, callback=Callback(),
                                    url=QWEN_ASR_URL, api_key="sk-offline-only")
        conversation.ws = SimpleNamespace(
            sock=SimpleNamespace(connected=True),
            send=lambda text: packets.append(json.loads(text)),
        )
        for language in (None, "zh", "en"):
            conversation.update_session(
                output_modalities=[Modality.TEXT],
                enable_input_audio_transcription=True, enable_turn_detection=True,
                turn_detection_type="server_vad", turn_detection_threshold=0.0,
                turn_detection_silence_duration_ms=700,
                transcription_params=Params(language=language, sample_rate=16000,
                                             input_audio_format="pcm"),
            )
            session = packets[-1]["session"]
            self.assertEqual(session["input_audio_format"], "pcm")
            self.assertEqual(session["sample_rate"], 16000)
            self.assertEqual(session["modalities"], ["text"])
            self.assertEqual(session["turn_detection"]["silence_duration_ms"], 700)
            if language is None:
                self.assertNotIn("language", session["input_audio_transcription"])
            else:
                self.assertEqual(session["input_audio_transcription"]["language"], language)
        self.assertEqual(conversation.apikey, "sk-offline-only")
        self.assertEqual(conversation.url, QWEN_ASR_URL + "?model=" + QWEN_ASR_MODEL)

    def test_privacy_filter_blocks_only_realtime_sdk_payload_logs(self):
        filter_ = _PrivateRealtimeLogs()
        secret = logging.LogRecord("dashscope", logging.ERROR,
            "C:\\site-packages\\dashscope\\audio\\qwen_omni\\omni_realtime.py", 1,
            "sk-secret transcript", (), None)
        self.assertFalse(filter_.filter(secret))
        ordinary = logging.LogRecord("dashscope", logging.INFO,
            "C:\\site-packages\\dashscope\\other.py", 1, "ordinary status", (), None)
        self.assertTrue(filter_.filter(ordinary))


if __name__ == "__main__":
    unittest.main()
