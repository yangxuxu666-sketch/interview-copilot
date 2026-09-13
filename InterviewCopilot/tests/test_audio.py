"""Synthetic audio tests. No real capture, model downloads, or external calls."""
import asyncio
from io import BytesIO
import math
from pathlib import Path
import queue
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

from interview_copilot.audio import (AudioError, AudioService, EnergySegmenter,
    SpeechChunk, _Run, _cloud_url, _replace_oldest, _wav_bytes)
from interview_copilot.local_asr import LocalASRCancelled, LocalASRError


RATE = 16000


def tone(ms, amplitude=10000, channels=1):
    values = [round(amplitude * math.sin(2 * math.pi * 440 * i / RATE))
              for i in range(round(RATE * ms / 1000))]
    if channels == 2:
        values = [v for sample in values for v in (sample, -sample)]
    return struct.pack("<" + "h" * len(values), *values)


def silence(ms, channels=1):
    return b"\0\0" * round(RATE * ms / 1000) * channels


def feed_ms(detector, duration, voiced, step=20):
    out = []
    for _ in range(duration // step):
        out.extend(detector.feed(tone(step) if voiced else silence(step)))
    return out


class SegmentTests(unittest.TestCase):
    def test_only_silence_and_short_click_are_rejected(self):
        detector = EnergySegmenter(RATE, 1)
        self.assertEqual(feed_ms(detector, 4000, False), [])
        self.assertEqual(feed_ms(detector, 100, True), [])
        self.assertEqual(feed_ms(detector, 1000, False), [])

    def test_speech_endpoint_preserves_preroll_and_trims_tail(self):
        detector = EnergySegmenter(RATE, 1, silence_ms=600)
        feed_ms(detector, 1000, False)
        self.assertEqual(feed_ms(detector, 500, True), [])
        self.assertEqual(feed_ms(detector, 580, False), [])
        result = feed_ms(detector, 20, False)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]), round(RATE * (0.24 + 0.5 + 0.15)) * 2)
        self.assertFalse(result[0].continuation)
        self.assertGreater(max(result[0]), 0)

    def test_continuous_speech_splits_at_strict_maximum(self):
        detector = EnergySegmenter(RATE, 1, max_segment_s=1)
        result = feed_ms(detector, 3400, True)
        result.extend(feed_ms(detector, 700, False))
        self.assertEqual(len(result), 4)
        self.assertTrue(all(len(chunk) <= RATE * 2 for chunk in result))
        self.assertEqual([chunk.continuation for chunk in result], [True, True, True, False])

    def test_large_pcm_chunk_respects_duration_bound(self):
        detector = EnergySegmenter(RATE, 1, max_segment_s=1)
        result = detector.feed(tone(3200))
        self.assertEqual(len(result), 3)
        self.assertTrue(all(len(chunk) == RATE * 2 for chunk in result))

    def test_exact_forced_boundary_emits_one_endpoint_after_silence(self):
        detector = EnergySegmenter(RATE, 1, max_segment_s=1, silence_ms=700)
        chunks = feed_ms(detector, 1000, True)
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0].continuation)
        self.assertEqual(feed_ms(detector, 680, False), [])
        markers = feed_ms(detector, 20, False)
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0], b"")
        self.assertTrue(markers[0].endpoint)
        self.assertFalse(markers[0].continuation)
        self.assertEqual(feed_ms(detector, 4000, False), [])

    def test_brief_tail_after_forced_cut_is_not_discarded(self):
        detector = EnergySegmenter(RATE, 1, max_segment_s=1, min_speech_ms=300)
        chunks = feed_ms(detector, 1060, True)
        chunks.extend(feed_ms(detector, 700, False))
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[0].continuation)
        self.assertFalse(chunks[1].continuation)
        self.assertGreater(len(chunks[1]), 0)
        self.assertEqual(feed_ms(detector, 1000, False), [])

    def test_speech_resumes_before_endpoint_without_false_marker(self):
        detector = EnergySegmenter(RATE, 1, max_segment_s=1)
        feed_ms(detector, 1000, True)
        self.assertEqual(feed_ms(detector, 500, False), [])
        self.assertEqual(feed_ms(detector, 100, True), [])
        chunks = feed_ms(detector, 700, False)
        self.assertEqual(len(chunks), 1)
        self.assertGreater(len(chunks[0]), 0)
        self.assertFalse(chunks[0].continuation)
        self.assertFalse(chunks[0].endpoint)

    def test_stereo_phase_does_not_cancel_energy(self):
        detector = EnergySegmenter(RATE, 2)
        for _ in range(20):
            detector.feed(tone(20, channels=2))
        self.assertGreater(detector.level, 0.1)
        result = []
        for _ in range(35):
            result.extend(detector.feed(silence(20, channels=2)))
        self.assertEqual(len(result), 1)

    def test_invalid_frames_and_reset(self):
        detector = EnergySegmenter(RATE, 2)
        with self.assertRaises(ValueError):
            detector.feed(b"\0\0")
        detector = EnergySegmenter(RATE, 1)
        feed_ms(detector, 500, True)
        detector.reset()
        self.assertEqual(feed_ms(detector, 700, False), [])
        self.assertEqual(detector.feed(b""), [])


class UtilityTests(unittest.TestCase):
    def test_wav_header_and_exact_pcm_roundtrip(self):
        pcm = tone(150, channels=2)
        with wave.open(BytesIO(_wav_bytes(pcm, RATE, 2)), "rb") as wav:
            self.assertEqual(wav.getframerate(), RATE)
            self.assertEqual(wav.getnchannels(), 2)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.readframes(wav.getnframes()), pcm)

    def test_queue_retains_newest_without_growing(self):
        pending = queue.Queue(maxsize=2)
        self.assertFalse(_replace_oldest(pending, "first"))
        self.assertFalse(_replace_oldest(pending, "second"))
        self.assertTrue(_replace_oldest(pending, "third"))
        self.assertEqual(pending.qsize(), 2)
        self.assertEqual([pending.get_nowait(), pending.get_nowait()], ["second", "third"])

    def test_cloud_url_rejects_credentials_in_url_and_non_tls(self):
        self.assertEqual(_cloud_url("https://asr.example/v1/audio/transcriptions"),
                         "https://asr.example/v1/audio/transcriptions")
        for bad in ["http://asr.example/audio/transcriptions", "file:///audio/transcriptions",
                    "https://user:secret@asr.example/audio/transcriptions",
                    "https://asr.example/chat/completions", "https://asr.example/audio/transcriptions#x"]:
            with self.subTest(url=bad), self.assertRaises(AudioError):
                _cloud_url(bad)


class FakeManager:
    devices = [
        {"index": 2, "name": "Microphone", "hostApi": 1, "maxInputChannels": 1,
         "defaultSampleRate": RATE, "isLoopbackDevice": False},
        {"index": 7, "name": "Headphones [Loopback]", "hostApi": 1, "maxInputChannels": 2,
         "defaultSampleRate": 48000, "isLoopbackDevice": True},
    ]
    opened = False
    terminated = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.terminate()

    def terminate(self):
        self.terminated = True

    def get_host_api_info_by_type(self, value):
        return {"index": 1}

    def get_default_wasapi_loopback(self):
        return self.devices[1]

    def get_device_info_by_index(self, index):
        return next(item for item in self.devices if item["index"] == index)

    def get_loopback_device_info_generator(self):
        return iter(self.devices)

    def open(self, **kwargs):
        self.opened = True
        raise AssertionError("Tests must never open an actual device")


class FakeModule:
    paWASAPI = 13
    manager = None

    def PyAudio(self):
        self.manager = FakeManager()
        return self.manager


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.events, self.transcripts = [], []
        self.service = AudioService(Path(self.temp.name), self.events.append, self.transcripts.append)
        self.service.configure({"asr_provider": "local"}, {})

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_construct_does_not_capture_or_load_models(self):
        self.assertFalse(self.service.status()["listening"])
        self.assertFalse(self.service.status()["model_ready"])
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_enumeration_excludes_physical_microphone_and_does_not_open(self):
        module = FakeModule()
        with patch.object(self.service, "_pyaudio", return_value=module):
            devices = self.service.devices()
        self.assertEqual([item["id"] for item in devices], [7])
        self.assertTrue(devices[0]["is_default"])
        self.assertFalse(module.manager.opened)
        self.assertTrue(module.manager.terminated)

    def test_caller_cannot_force_a_microphone_input(self):
        module = FakeModule()
        with patch.object(self.service, "_pyaudio", return_value=module):
            with self.assertRaisesRegex(AudioError, "不能选择麦克风"):
                self.service.start(device_id=2)
        self.assertFalse(module.manager.opened)
        self.assertFalse(self.service.status()["listening"])

    def test_capture_owns_manager_and_stream_on_its_own_thread(self):
        opened, closed = threading.Event(), threading.Event()
        managers = []
        class Stream:
            def is_active(self):
                return True
            def stop_stream(self):
                pass
            def close(self):
                closed.set()
        class Manager(FakeManager):
            def __init__(self):
                self.created_thread = threading.get_ident()
                self.opened_thread = None
                self.terminated_thread = None
                managers.append(self)
            def open(self, **kwargs):
                self.opened_thread = threading.get_ident()
                opened.set()
                return Stream()
            def terminate(self):
                self.terminated_thread = threading.get_ident()
        class Module(FakeModule):
            paInt16, paComplete, paContinue = 8, 1, 0
            def PyAudio(self):
                return Manager()
        with patch.object(self.service, "_pyaudio", return_value=Module()), \
                patch.object(self.service, "_recognize"):
            self.service.start()
            self.assertTrue(opened.wait(2))
            self.service.stop()
        self.assertTrue(closed.is_set())
        self.assertEqual(len(managers), 2)
        initial, capture = managers
        self.assertIsNone(initial.opened_thread)
        self.assertEqual(initial.created_thread, initial.terminated_thread)
        self.assertNotEqual(initial.created_thread, capture.created_thread)
        self.assertEqual(capture.created_thread, capture.opened_thread)
        self.assertEqual(capture.created_thread, capture.terminated_thread)

    def test_device_changed_to_microphone_before_capture_never_opens(self):
        initial, capture = FakeManager(), FakeManager()
        capture.devices = [{**FakeManager.devices[0], "index": 7}]
        class Module(FakeModule):
            def PyAudio(self):
                return capture
        run = _Run({"asr_provider": "cloud"}, {}, initial.devices[1])
        self.service._run = run
        self.service._capture(run, Module())
        self.assertFalse(capture.opened)
        self.assertTrue(capture.terminated)
        self.assertFalse(self.service.status()["listening"])
        self.assertEqual(self.service.status()["state"], "error")
        self.assertIn("播放设备在启动时发生变化", self.service.status()["message"])

    def test_stop_suppresses_in_flight_result_and_clears_buffer(self):
        entered, release = threading.Event(), threading.Event()
        run = _Run({"asr_provider": "cloud"}, {}, FakeManager.devices[1])
        self.service._run = run
        run.segments.put(SpeechChunk(tone(400), continuation=True))
        run.segments.put(tone(400))
        def recognize(wav, passed_run):
            entered.set()
            release.wait(3)
            return "不得在停止后显示"
        with patch.object(self.service, "_transcribe_cloud", side_effect=recognize):
            worker = threading.Thread(target=self.service._recognize, args=(run,))
            worker.start()
            self.assertTrue(entered.wait(2))
            self.service.stop()
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.transcripts, [])
        self.assertEqual(run.segments.qsize(), 0)
        self.assertFalse(self.service.status()["listening"])
        self.assertEqual(self.service.status()["asr_state"], "idle")

    def test_inference_failure_stops_listening_and_reports_error(self):
        run = _Run({"asr_provider": "cloud"}, {}, FakeManager.devices[1])
        self.service._run = run
        run.segments.put(tone(400))
        with patch.object(self.service, "_transcribe_cloud", side_effect=AudioError("服务不可用")):
            self.service._recognize(run)
        self.assertFalse(self.service.status()["listening"])
        self.assertEqual(self.service.status()["state"], "error")
        self.assertTrue(run.stop.is_set())
        self.assertTrue(any(item.get("message") == "服务不可用" for item in self.events))

    def test_empty_endpoint_waits_for_preceding_asr_without_another_request(self):
        entered, release, endpoint_seen = threading.Event(), threading.Event(), threading.Event()
        detector = EnergySegmenter(RATE, 1, max_segment_s=1)
        chunks = feed_ms(detector, 1000, True) + feed_ms(detector, 700, False)
        self.assertEqual(len(chunks), 2)
        run = _Run({"asr_provider": "cloud"}, {}, FakeManager.devices[1])
        self.service._run = run
        for chunk in chunks:
            _replace_oldest(run.segments, chunk)
        def recognize(wav, passed_run):
            entered.set()
            release.wait(3)
            return "最后一句刚好到达切片边界"
        def receive(value):
            self.transcripts.append(value)
            if value.get("endpoint"):
                endpoint_seen.set()
                run.stop.set()
        self.service.on_transcript = receive
        with patch.object(self.service, "_transcribe_cloud", side_effect=recognize) as transcribe:
            worker = threading.Thread(target=self.service._recognize, args=(run,))
            worker.start()
            self.assertTrue(entered.wait(2))
            self.assertFalse(endpoint_seen.is_set())
            self.assertEqual(self.transcripts, [])
            release.set()
            self.assertTrue(endpoint_seen.wait(2))
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(transcribe.call_count, 1)
        self.assertEqual(len(self.transcripts), 2)
        self.assertTrue(self.transcripts[0]["continuation"])
        self.assertEqual(self.transcripts[1], {"text": "", "asr_ms": 0,
                                             "continuation": False, "endpoint": True})

    def test_unrecognized_final_tail_still_emits_endpoint(self):
        run = _Run({"asr_provider": "cloud"}, {}, FakeManager.devices[1])
        self.service._run = run
        run.segments.put(SpeechChunk(tone(60), continuation=False))
        def receive(value):
            self.transcripts.append(value)
            run.stop.set()
        self.service.on_transcript = receive
        with patch.object(self.service, "_transcribe_cloud", return_value=""):
            self.service._recognize(run)
        self.assertEqual(len(self.transcripts), 1)
        self.assertTrue(self.transcripts[0]["endpoint"])
        self.assertEqual(self.transcripts[0]["text"], "")

    def test_local_auto_language_is_none(self):
        class Model:
            kwargs = None
            def transcribe(self, source, **kwargs):
                self.source = source
                self.kwargs = kwargs
                return "中文 English"
        model = Model()
        run = _Run({"asr_language": "auto"}, {}, FakeManager.devices[1])
        wav = _wav_bytes(silence(100), RATE, 1)
        text = self.service._transcribe_local(model, wav, run)
        self.assertEqual(text, "中文 English")
        self.assertIs(model.source, wav)
        self.assertIsNone(model.kwargs["language"])
        self.assertIs(model.kwargs["cancel"], run.stop)

    def test_local_crash_stops_listening_without_stopping_service(self):
        class Model:
            ready = False
            closed = False
            def transcribe(self, *args, **kwargs):
                raise LocalASRError("本地语音识别子进程已退出（代码 0xC0000005）。")
            def close(self):
                self.closed = True
        model = Model()
        run = _Run({"asr_provider": "local"}, {}, FakeManager.devices[1])
        self.service._run = run
        self.service._model = model
        self.service._model_name = "base"
        run.segments.put(tone(400))
        with patch.object(self.service, "_get_model", return_value=model):
            self.service._recognize(run)
        self.assertFalse(self.service.status()["listening"])
        self.assertEqual(self.service.status()["asr_state"], "error")
        self.assertFalse(self.service.status()["model_ready"])
        self.assertTrue(model.closed)
        self.assertFalse(self.service._closed)
        self.assertIn("0xC0000005", self.service.status()["asr_message"])

    def test_stop_terminates_local_worker_and_drops_result(self):
        entered, released = threading.Event(), threading.Event()
        class Model:
            ready = True
            def transcribe(self, *args, **kwargs):
                entered.set()
                released.wait(3)
                return "不应显示的旧结果"
            def close(self):
                self.ready = False
                released.set()
        model = Model()
        run = _Run({"asr_provider": "local"}, {}, FakeManager.devices[1])
        self.service._run = run
        self.service._model = model
        self.service._model_name = "base"
        run.segments.put(tone(400))
        with patch.object(self.service, "_get_model", return_value=model):
            thread = threading.Thread(target=self.service._recognize, args=(run,))
            thread.start()
            self.assertTrue(entered.wait(2))
            self.service.stop()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(released.is_set())
        self.assertEqual(self.transcripts, [])
        self.assertFalse(self.service.status()["model_ready"])
        self.assertEqual(self.service.status()["asr_state"], "idle")

    def test_model_change_cancels_warmup_and_suppresses_old_error(self):
        entered, released = threading.Event(), threading.Event()
        class Model:
            ready = False
            def __init__(self, *args):
                pass
            def initialize(self, cancel):
                entered.set()
                released.wait(3)
                raise LocalASRCancelled("本地语音识别已取消。")
            def close(self):
                released.set()
        with patch("interview_copilot.audio.LocalASRWorker", Model):
            self.service.warmup()
            thread = self.service._warmup_thread
            self.assertTrue(entered.wait(2))
            self.service.configure({"asr_provider": "local", "whisper_model": "tiny"}, {})
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(released.is_set())
        self.assertEqual(self.service.status()["model"], "tiny")
        self.assertFalse(self.service.status()["model_ready"])
        self.assertEqual(self.service.status()["asr_state"], "idle")
        self.assertFalse(any(event.get("state") == "error" for event in self.events))

    def test_warmup_failure_never_reports_ready(self):
        with patch.object(self.service, "_get_model", side_effect=AudioError("组件加载失败")):
            self.service.warmup()
            self.service._warmup_thread.join(2)
        self.assertEqual(self.service.status()["asr_state"], "error")
        self.assertFalse(self.service.status()["model_ready"])
        self.assertFalse(any(event.get("state") == "ready" for event in self.events))

    def test_closed_service_rejects_restart(self):
        self.service.close()
        with self.assertRaisesRegex(AudioError, "已关闭"):
            self.service.start()


class CloudTests(unittest.TestCase):
    def make_run(self):
        return _Run({"asr_provider": "cloud", "cloud_asr_url": "https://example.test/v1/audio/transcriptions",
                     "cloud_asr_model": "speech-model", "asr_language": "auto"},
                    {"cloud_asr_key": "test-secret"}, FakeManager.devices[1])

    def test_cloud_multipart_and_auto_language(self):
        import httpx
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"text": "Tell me about your project."})
        original = httpx.AsyncClient
        def client(**kwargs):
            self.assertFalse(kwargs["follow_redirects"])
            return original(transport=httpx.MockTransport(respond), **kwargs)
        with patch("httpx.AsyncClient", side_effect=client):
            result = AudioService._transcribe_cloud(_wav_bytes(tone(400), RATE, 1), self.make_run())
        self.assertEqual(result, "Tell me about your project.")
        self.assertEqual(requests[0].headers["authorization"], "Bearer test-secret")
        self.assertIn(b'filename="segment.wav"', requests[0].content)
        self.assertNotIn(b'name="language"', requests[0].content)
        self.assertIn(b'name="response_format"', requests[0].content)

    def test_sensevoice_compatibility_matches_only_official_endpoint_and_model(self):
        import httpx
        original = httpx.AsyncClient
        sensevoice = "FunAudioLLM/SenseVoiceSmall"
        cases = [
            ("https://api.siliconflow.cn/v1/audio/transcriptions", sensevoice, True),
            ("https://api.siliconflow.com/v1/audio/transcriptions", sensevoice, True),
            ("https://api.siliconflow.cn.example.test/v1/audio/transcriptions", sensevoice, False),
            ("https://custom.api.siliconflow.cn/v1/audio/transcriptions", sensevoice, False),
            ("https://example.test/v1/audio/transcriptions", sensevoice, False),
            ("https://api.siliconflow.cn/custom/audio/transcriptions", sensevoice, False),
            ("https://api.siliconflow.cn/v1/audio/transcriptions/", sensevoice, False),
            ("https://api.siliconflow.cn/v1/audio/transcriptions", "another-model", False),
            ("https://api.siliconflow.cn/v1/audio/transcriptions", sensevoice.lower(), False),
        ]
        for url, model, minimal_fields in cases:
            with self.subTest(url=url, model=model):
                requests = []

                def respond(request):
                    requests.append(request)
                    return httpx.Response(200, json={"text": "请介绍你的项目经验。"})

                run = self.make_run()
                run.settings.update(cloud_asr_url=url, cloud_asr_model=model, asr_language="zh")
                transport = httpx.MockTransport(respond)
                with patch("httpx.AsyncClient", side_effect=lambda **kw: original(transport=transport, **kw)):
                    text = AudioService._transcribe_cloud(b"test-wav", run)
                self.assertEqual(text, "请介绍你的项目经验。")
                content = requests[0].content
                self.assertIn(b'name="file"; filename="segment.wav"', content)
                self.assertIn(b'name="model"', content)
                self.assertIn(model.encode(), content)
                self.assertEqual(b'name="response_format"' in content, not minimal_fields)
                self.assertEqual(b'name="language"' in content, not minimal_fields)

    def test_authentication_error_never_echoes_secret_or_response_body(self):
        import httpx
        original = httpx.AsyncClient
        transport = httpx.MockTransport(lambda request: httpx.Response(401, text="test-secret"))
        with patch("httpx.AsyncClient", side_effect=lambda **kw: original(transport=transport, **kw)):
            with self.assertRaisesRegex(AudioError, "鉴权失败") as caught:
                AudioService._transcribe_cloud(b"wav", self.make_run())
        self.assertNotIn("test-secret", str(caught.exception))

    def test_stop_cancels_in_flight_http_request(self):
        import httpx
        entered = threading.Event()
        original = httpx.AsyncClient
        async def respond(request):
            entered.set()
            await asyncio.sleep(30)
            return httpx.Response(200, json={"text": "late"})
        transport = httpx.MockTransport(respond)
        run = self.make_run()
        values = []
        service = AudioService(Path("unused"), lambda event: None, lambda value: None)
        service._run = run
        with patch("httpx.AsyncClient", side_effect=lambda **kw: original(transport=transport, **kw)):
            worker = threading.Thread(target=lambda: values.append(AudioService._transcribe_cloud(b"wav", run)))
            worker.start()
            self.assertTrue(entered.wait(2))
            service.stop()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(values, [""])


if __name__ == "__main__":
    unittest.main()
