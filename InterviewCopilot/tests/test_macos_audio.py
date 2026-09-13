"""ScreenCaptureKit contract tests using fake bindings, never real Mac capture."""
from contextlib import nullcontext
import queue
import struct
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from interview_copilot import macos_audio as mac
from interview_copilot.audio import _replace_oldest


def description(**overrides):
    values = dict(mFormatID=int.from_bytes(b"lpcm", "big"), mSampleRate=16000,
                  mChannelsPerFrame=1, mFormatFlags=9, mBitsPerChannel=32,
                  mBytesPerFrame=4)
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeCM:
    @staticmethod
    def CMSampleBufferIsValid(sample):
        return sample.get("valid", True)

    @staticmethod
    def CMSampleBufferDataIsReady(sample):
        return sample.get("ready", True)

    @staticmethod
    def CMSampleBufferGetFormatDescription(sample):
        return sample.get("description", description())

    @staticmethod
    def CMAudioFormatDescriptionGetStreamBasicDescription(value):
        return value

    @staticmethod
    def CMSampleBufferGetNumSamples(sample):
        return sample.get("frames", len(sample["data"]) // 4)

    @staticmethod
    def CMSampleBufferGetDataBuffer(sample):
        return sample["data"]

    @staticmethod
    def CMBlockBufferGetDataLength(block):
        return len(block)

    @staticmethod
    def CMBlockBufferCopyDataBytes(block, offset, size, destination):
        return (0, block[offset:offset + size])

    @staticmethod
    def CMTimeMake(value, scale):
        return (value, scale)


class FakeHandler:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self


class FakeConfig(FakeHandler):
    def __getattr__(self, name):
        if name.startswith("set"):
            return lambda value: setattr(self, name[3:-1], value)
        raise AttributeError(name)


class FakeFilter(FakeHandler):
    def initWithDisplay_excludingApplications_exceptingWindows_(self, display, apps, windows):
        self.display, self.apps, self.windows = display, apps, windows
        return self


def fake_frameworks(*, content_error=None, start_error=None, delayed_content=False,
                    delayed_start=False, displays=None):
    calls = SimpleNamespace(content=0, stops=0, start=None, content_done=None,
                            outputs=[], native=None, config=None)

    class Content:
        @staticmethod
        def getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
                exclude_desktop, only_onscreen, done):
            calls.content += 1
            calls.content_done = done
            if not delayed_content:
                done(SimpleNamespace(displays=lambda: [object()] if displays is None else displays),
                     content_error)

    class Native(FakeHandler):
        def initWithFilter_configuration_delegate_(self, content_filter, config, delegate):
            self.delegate = delegate
            calls.config = config
            calls.native = self
            return self

        def addStreamOutput_type_sampleHandlerQueue_error_(self, handler, kind, dispatch_queue, error):
            calls.outputs.append(kind)
            return True, None

        def startCaptureWithCompletionHandler_(self, done):
            calls.start = done
            if not delayed_start:
                done(start_error)

        def stopCaptureWithCompletionHandler_(self, done):
            calls.stops += 1
            done(None)

    return SimpleNamespace(objc=SimpleNamespace(autorelease_pool=nullcontext),
        CM=FakeCM, dispatch=SimpleNamespace(dispatch_queue_create=lambda *_: object()),
        SCK=SimpleNamespace(SCShareableContent=Content, SCContentFilter=FakeFilter,
                            SCStreamConfiguration=FakeConfig, SCStream=Native,
                            SCStreamOutputTypeAudio=1), calls=calls)


class PCMTests(unittest.TestCase):
    def test_float32_clips_endpoints_and_nonfinite_without_changing_duration(self):
        values = [-2, -1, -0.5, 0, 0.5, 1, 2, float("nan"), float("inf")]
        pcm = mac._pcm16_from_payload(struct.pack("<9f", *values), description(), 9)
        self.assertEqual(struct.unpack("<9h", pcm),
                         (-32768, -32768, -16384, 0, 16384, 32767, 32767, 0, 0))

    def test_big_endian_pcm16_is_copied_as_little_endian(self):
        desc = description(mFormatFlags=14, mBitsPerChannel=16, mBytesPerFrame=2)
        result = mac._pcm16_from_payload(struct.pack(">3h", -20000, 0, 30000), desc, 3)
        self.assertEqual(result, struct.pack("<3h", -20000, 0, 30000))

    def test_extreme_float64_values_clip_before_integer_conversion(self):
        desc = description(mBitsPerChannel=64, mBytesPerFrame=8)
        result = mac._pcm16_from_payload(struct.pack("<2d", 1e308, -1e308), desc, 2)
        self.assertEqual(struct.unpack("<2h", result), (32767, -32768))

    def test_mono_planar_float_buffer_uses_identical_frame_order(self):
        desc = description(mFormatFlags=9 | 32)
        result = mac._pcm16_from_payload(struct.pack("<2f", 0.5, -0.25), desc, 2)
        self.assertEqual(struct.unpack("<2h", result), (16384, -8192))

    def test_format_changes_are_rejected_instead_of_misinterpreted(self):
        for change in ({"mSampleRate": 48000}, {"mChannelsPerFrame": 2},
                       {"mFormatID": 0}, {"mBytesPerFrame": 8},
                       {"mFormatFlags": 0}, {"mFormatFlags": 9 | 16}):
            with self.subTest(change=change), self.assertRaises(mac.MacOSAudioError):
                mac._pcm16_from_payload(b"\0" * 4, description(**change), 1)
        with self.assertRaises(mac.MacOSAudioError):
            mac._pcm16_from_payload(b"\0" * 3, description(), 1)

    def test_oversized_native_buffer_is_rejected_before_copy(self):
        sample = {"data": b"\0" * (mac.RATE * 8 + 1), "frames": mac.RATE}
        with patch.object(FakeCM, "CMBlockBufferCopyDataBytes") as copy:
            with self.assertRaises(mac.MacOSAudioError):
                mac._sample_pcm16(sample, FakeCM)
            copy.assert_not_called()

    def test_not_ready_native_buffer_is_ignored(self):
        self.assertEqual(mac._sample_pcm16({"ready": False}, FakeCM), b"")

    def test_zero_frames_do_not_allocate_an_empty_native_output(self):
        with patch.object(FakeCM, "CMBlockBufferCopyDataBytes") as copy:
            self.assertEqual(mac._sample_pcm16({"data": b"", "frames": 0}, FakeCM), b"")
            copy.assert_not_called()


class StreamTests(unittest.TestCase):
    def manager(self, frameworks):
        with patch.object(mac, "_load_frameworks", return_value=frameworks):
            return mac.PyAudio()

    def open(self, manager, callback=None, cancel=None):
        return manager.open(format=mac.paInt16, channels=1, rate=16000,
            input=True, input_device_index=0, frames_per_buffer=128,
            stream_callback=callback or (lambda *_: (None, mac.paContinue)),
            cancel_event=cancel)

    def setUp(self):
        self.handler = patch.object(mac, "_output_class", return_value=FakeHandler)
        self.handler.start()
        self.addCleanup(self.handler.stop)

    def test_enumeration_never_requests_recording_permission_or_a_microphone(self):
        f = fake_frameworks()
        with self.manager(f) as manager:
            devices = list(manager.get_loopback_device_info_generator())
            self.assertEqual(len(devices), 1)
            self.assertEqual(devices[0]["index"], 0)
            self.assertTrue(devices[0]["isLoopbackDevice"])
            self.assertEqual(f.calls.content, 0)
            with self.assertRaises(mac.MacOSAudioError):
                manager.get_device_info_by_index(1)

    def test_only_system_audio_output_is_registered_and_pcm_callback_is_bounded(self):
        f = fake_frameworks()
        pending = queue.Queue(maxsize=2)
        with self.manager(f) as manager:
            def callback(data, frames, info, flags):
                self.assertLessEqual(frames, 128)
                _replace_oldest(pending, data)
                return None, mac.paContinue
            stream = self.open(manager, callback)
            self.assertTrue(stream.is_active())
            self.assertEqual(f.calls.outputs, [1])
            self.assertTrue(f.calls.config.CapturesAudio)
            self.assertFalse(f.calls.config.CaptureMicrophone)
            self.assertTrue(f.calls.config.ExcludesCurrentProcessAudio)
            self.assertEqual(f.calls.config.SampleRate, 16000)
            self.assertEqual(f.calls.config.ChannelCount, 1)
            sample = {"data": struct.pack("<400f", *([0.5] * 400))}
            stream._receive(sample, 0)  # Screen frames cannot enter audio pipeline.
            stream._receive(sample, 2)  # Microphone frames cannot enter pipeline.
            self.assertTrue(pending.empty())
            stream._receive(sample, 1)
            self.assertEqual(pending.qsize(), 2)
            self.assertEqual(len(pending.get_nowait()), 256)
            self.assertEqual(len(pending.get_nowait()), 32)
        self.assertEqual(f.calls.stops, 1)
        self.assertFalse(stream.is_active())
        stream._receive(sample, 1)
        self.assertTrue(pending.empty())
        stream.close()
        self.assertEqual(f.calls.stops, 1)

    def test_permission_denial_is_actionable_without_starting_stream(self):
        f = fake_frameworks(content_error=SimpleNamespace(code=lambda: -3801))
        with self.manager(f) as manager:
            with self.assertRaisesRegex(mac.MacOSAudioError, "隐私与安全性"):
                self.open(manager)
            self.assertEqual(f.calls.outputs, [])
            self.assertEqual(manager._streams, [])

    def test_native_start_failure_releases_started_resources(self):
        f = fake_frameworks(start_error=SimpleNamespace(code=lambda: -3802))
        with self.manager(f) as manager:
            with self.assertRaisesRegex(mac.MacOSAudioError, "无法启动"):
                self.open(manager)
            self.assertEqual(f.calls.stops, 1)
            self.assertIsNone(f.calls.native.delegate._owner)

    def test_cancel_before_start_does_not_request_permission(self):
        f = fake_frameworks()
        cancel = threading.Event()
        cancel.set()
        with self.manager(f) as manager:
            with self.assertRaisesRegex(mac.MacOSAudioError, "取消"):
                self.open(manager, cancel=cancel)
            self.assertEqual(f.calls.content, 0)

    def test_cancel_during_start_stops_again_if_native_completion_arrives_late(self):
        f = fake_frameworks(delayed_start=True)
        cancel = threading.Event()
        manager = self.manager(f)
        result = []
        def start():
            try:
                self.open(manager, cancel=cancel)
            except mac.MacOSAudioError as error:
                result.append(str(error))
        worker = threading.Thread(target=start)
        worker.start()
        for _ in range(100):
            if f.calls.start is not None:
                break
            threading.Event().wait(0.005)
        self.assertIsNotNone(f.calls.start)
        cancel.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertIn("取消", result[0])
        self.assertEqual(f.calls.stops, 1)
        f.calls.start(None)
        self.assertEqual(f.calls.stops, 2)
        manager.terminate()

    def test_conversion_failure_survives_native_callback_boundary(self):
        f = fake_frameworks()
        with self.manager(f) as manager:
            stream = self.open(manager)
            stream._receive({"data": b"123", "frames": 1}, 1)
            with self.assertRaisesRegex(mac.MacOSAudioError, "音频帧不完整"):
                stream.is_active()

    def test_async_wait_timeout_bounds_wait_and_cleans_late_completion(self):
        f = fake_frameworks()
        stream = mac._SystemAudioStream(f, lambda *_: None, 128)
        completions = []
        cleanup = []
        with self.assertRaisesRegex(mac.MacOSAudioError, "超时"):
            stream._wait(completions.append, timeout=0,
                         late_cleanup=lambda: cleanup.append(True))
        completions[0](None)
        self.assertEqual(cleanup, [True])

    def test_old_macos_fails_before_importing_native_frameworks(self):
        with patch.object(mac.sys, "platform", "darwin"), \
                patch.object(mac.platform, "mac_ver", return_value=("12.7", (), "arm64")), \
                patch.object(mac.importlib, "import_module") as importer:
            with self.assertRaisesRegex(mac.MacOSAudioError, "macOS 13"):
                mac.PyAudio()
            importer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
