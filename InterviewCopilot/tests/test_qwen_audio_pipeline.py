"""WASAPI-to-realtime lifecycle tests with fake capture and fake cloud service."""
from array import array
import math
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from interview_copilot.audio import AudioError, AudioService, PCM16StreamConverter, _Run
from interview_copilot.qwen_asr import QwenASRCancelled, QwenASRError
from tests.test_audio import FakeManager


def source_pcm(frames, rate=48000, channels=2, opposite=False):
    samples = array("h")
    for index in range(frames):
        value = round(7000 * math.sin(2 * math.pi * 440 * index / rate))
        samples.extend((value, -value if opposite else value) if channels == 2 else (value,))
    return samples.tobytes()


class ConversionTests(unittest.TestCase):
    def test_ratecv_state_preserves_every_sample_across_irregular_callbacks(self):
        for rate in (16000, 44100, 48000):
            pcm = source_pcm(rate, rate)
            expected = PCM16StreamConverter(rate, 2).feed(pcm)
            converter = PCM16StreamConverter(rate, 2)
            step = 997 * 4
            actual = b"".join(converter.feed(pcm[index:index + step])
                               for index in range(0, len(pcm), step))
            self.assertEqual(actual, expected)
            self.assertEqual(len(actual), 16000 * 2)

    def test_realtime_opposite_phase_stereo_does_not_cancel(self):
        converted = PCM16StreamConverter(48000, 2).feed(source_pcm(4800, opposite=True))
        self.assertGreater(max(abs(v) for v in array("h", converted)), 6500)

    def test_partial_pcm_frame_is_rejected(self):
        with self.assertRaises(AudioError):
            PCM16StreamConverter(48000, 2).feed(b"\0\0")


class FakeRealtime:
    def __init__(self, api_key, *, language, silence_ms, on_final, on_partial):
        self.language, self.silence_ms = language, silence_ms
        self.final, self.partial = on_final, on_partial
        self.connected = threading.Event()
        self.sent = threading.Event()
        self.closed = threading.Event()
        self.packets = []
        self.block_connect = False
        self.block_send = False
        self.failure = None

    def connect(self, cancel=None):
        self.connected.set()
        if self.block_connect:
            self.closed.wait(3)
            raise QwenASRCancelled("实时识别已停止。")

    def send_audio(self, packet):
        self.packets.append(packet)
        self.sent.set()
        if self.block_send:
            self.closed.wait(3)

    def check_error(self):
        if self.failure:
            raise QwenASRError(self.failure)

    def close(self):
        self.closed.set()


class FakeStream:
    def __init__(self, callback):
        self.callback = callback
        self.active = True

    def is_active(self):
        return self.active

    def stop_stream(self):
        self.active = False

    def close(self):
        self.active = False


class CaptureModule:
    paWASAPI, paInt16, paComplete, paContinue = 13, 8, 1, 0

    def __init__(self):
        self.opened = threading.Event()
        self.stream = None
        self.managers = []

    def PyAudio(self):
        owner = self
        class Manager(FakeManager):
            def open(self, **kwargs):
                owner.stream = FakeStream(kwargs["stream_callback"])
                owner.opened.set()
                return owner.stream
        manager = Manager()
        self.managers.append(manager)
        return manager


class RealtimePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.events, self.transcripts = [], []
        self.service = AudioService(Path(self.temp.name), self.events.append, self.transcripts.append)
        self.service.configure({"asr_provider": "qwen", "asr_language": "zh", "silence_ms": 700},
                               {"cloud_asr_key": "test-placeholder"})
        self.module = CaptureModule()
        self.clients = []
        self.client_options = {}
        def make_client(*args, **kwargs):
            client = FakeRealtime(*args, **kwargs)
            for key, value in self.client_options.items():
                setattr(client, key, value)
            self.clients.append(client)
            return client
        self.capture_patch = patch.object(self.service, "_pyaudio", return_value=self.module)
        self.client_patch = patch("interview_copilot.qwen_asr.QwenASRStream", side_effect=make_client)
        self.capture_patch.start()
        self.client_patch.start()

    def tearDown(self):
        self.service.close()
        self.client_patch.stop()
        self.capture_patch.stop()
        self.temp.cleanup()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(predicate())

    def test_missing_key_is_rejected_before_audio_device_access(self):
        self.service.configure({"asr_provider": "qwen"}, {})
        with self.assertRaisesRegex(AudioError, "百炼"):
            self.service.start()
        self.assertEqual(self.module.managers, [])
        self.assertEqual(self.clients, [])

    def test_continuous_packets_are_sent_before_utterance_endpoint_and_only_final_delivers(self):
        self.service.start()
        self.assertTrue(self.module.opened.wait(2))
        client = self.clients[0]
        pcm = source_pcm(14400, opposite=True)  # 300 ms, no endpoint silence.
        self.module.stream.callback(pcm, 14400, {}, 0)
        self.assertTrue(client.sent.wait(2))
        self.wait_for(lambda: len(client.packets) >= 3)
        self.assertTrue(all(len(packet) == 3200 for packet in client.packets))
        self.assertGreater(max(abs(v) for v in array("h", client.packets[0])), 6500)
        self.assertEqual(self.transcripts, [])
        client.partial("请介绍一下")
        self.assertEqual(self.transcripts, [])
        self.assertTrue(any(event.get("type") == "transcript_partial" for event in self.events))
        client.final("请介绍一下你上一份工作。")
        self.assertEqual(len(self.transcripts), 1)
        self.assertEqual(self.transcripts[0]["text"], "请介绍一下你上一份工作。")
        self.assertFalse(self.transcripts[0]["continuation"])
        self.assertIsNone(self.transcripts[0]["asr_ms"])
        run = self.service._run
        self.service.stop()
        self.assertTrue(client.closed.is_set())
        self.assertTrue(run.qwen_audio.empty())
        count = len(self.transcripts)
        client.final("停止后的旧结果")
        self.service._run = _Run({"asr_provider": "qwen"}, {}, FakeManager.devices[1])
        client.final("上一代的结果")
        self.assertEqual(len(self.transcripts), count)

    def test_stop_during_connect_does_not_open_capture_or_emit_old_results(self):
        self.client_options["block_connect"] = True
        self.service.start()
        self.wait_for(lambda: bool(self.clients))
        client = self.clients[0]
        self.assertTrue(client.connected.wait(2))
        self.service.stop()
        self.assertTrue(client.closed.is_set())
        self.assertFalse(self.module.opened.is_set())
        self.assertFalse(self.service.status()["listening"])
        self.assertEqual(self.service.status()["asr_state"], "idle")

    def test_network_backlog_is_bounded_and_disconnects_instead_of_dropping_words(self):
        self.client_options["block_send"] = True
        self.service.start()
        self.assertTrue(self.module.opened.wait(2))
        run = self.service._run
        pcm = source_pcm(96000)  # Faster than a stalled network can consume.
        self.module.stream.callback(pcm, 96000, {}, 0)
        self.wait_for(lambda: self.service.status()["state"] == "error")
        self.assertIn("积压", self.service.status()["message"])
        self.assertFalse(self.service.status()["listening"])
        self.assertTrue(run.stop.is_set())
        self.assertTrue(run.qwen_audio.empty())
        self.assertTrue(self.clients[0].closed.is_set())
        self.assertFalse(self.service._closed)

    def test_cloud_error_stops_this_run_but_keeps_application_available(self):
        self.client_options["failure"] = "阿里云语音识别鉴权失败。"
        self.service.start()
        self.wait_for(lambda: self.service.status()["state"] == "error")
        self.assertIn("鉴权失败", self.service.status()["asr_message"])
        self.assertFalse(self.service.status()["listening"])
        self.assertFalse(self.service._closed)
        self.assertTrue(self.clients[0].closed.is_set())


if __name__ == "__main__":
    unittest.main()
