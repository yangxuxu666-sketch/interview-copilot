"""Platform contract checks only; native Mac capture needs a real Mac."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

from interview_copilot import app, audio


class DesktopCapabilityTests(unittest.TestCase):
    def test_supported_mac_checks_components_without_requesting_capture(self):
        with patch.object(app.sys, 'platform', 'darwin'), \
                patch.object(app.platform, 'mac_ver', return_value=('13.7', '', 'arm64')), \
                patch.object(app.importlib.util, 'find_spec', return_value=object()) as found:
            result = app.desktop_capabilities()
        self.assertEqual(result['platform'], 'macOS')
        self.assertTrue(result['audio'])
        self.assertIn('权限', result['audio_note'])
        self.assertEqual({c.args[0] for c in found.call_args_list},
                         {'objc', 'ScreenCaptureKit', 'CoreAudio', 'CoreMedia', 'dispatch'})

    def test_older_mac_reports_audio_unavailable(self):
        with patch.object(app.sys, 'platform', 'darwin'), \
                patch.object(app.platform, 'mac_ver', return_value=('12.7', '', 'x86_64')), \
                patch.object(app.importlib.util, 'find_spec') as found:
            self.assertFalse(app.desktop_capabilities()['audio'])
        found.assert_not_called()

    def test_missing_mac_component_does_not_claim_audio_ready(self):
        with patch.object(app.sys, 'platform', 'darwin'), \
                patch.object(app.platform, 'mac_ver', return_value=('15.0', '', 'arm64')), \
                patch.object(app.importlib.util, 'find_spec', return_value=None):
            self.assertFalse(app.desktop_capabilities()['audio'])

    def test_windows_still_uses_wasapi_component(self):
        with patch.object(app.sys, 'platform', 'win32'), \
                patch.object(app.importlib.util, 'find_spec', return_value=object()) as found:
            result = app.desktop_capabilities()
        self.assertEqual(result, {'platform': 'Windows', 'audio': True, 'audio_note': ''})
        found.assert_called_once_with('pyaudiowpatch')


class MacCaptureIntegrationTests(unittest.TestCase):
    def test_start_passes_cancellation_into_native_capture_without_changing_pipeline(self):
        captured = {}
        device = {'index': 0, 'name': 'macOS system playback', 'hostApi': 0,
                  'isLoopbackDevice': True, 'maxInputChannels': 1, 'defaultSampleRate': 16000}
        class Stream:
            def stop_stream(self): pass
            def close(self): captured['closed'] = True
        class Manager:
            def get_host_api_info_by_type(self, kind): return {'index': 0}
            def get_device_info_by_index(self, index): return dict(device)
            def open(self, **kwargs):
                captured.update(kwargs)
                kwargs['cancel_event'].set()
                return Stream()
            def terminate(self): captured['terminated'] = True
        module = SimpleNamespace(PyAudio=Manager, paWASAPI=0, paInt16=8, paComplete=1, paContinue=0)
        with tempfile.TemporaryDirectory() as folder:
            service = audio.AudioService(Path(folder), lambda _: None, lambda _: None)
            run = audio._Run({'asr_provider': 'cloud'}, {}, device)
            service._run = run
            with patch.object(audio.sys, 'platform', 'darwin'):
                service._capture(run, module)
            service.close()
        self.assertIs(captured['cancel_event'], run.stop)
        self.assertEqual(captured['input_device_index'], 0)
        self.assertTrue(captured['closed'])
        self.assertTrue(captured['terminated'])


if __name__ == '__main__':
    unittest.main()
