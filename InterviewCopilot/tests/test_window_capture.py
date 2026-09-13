"""Verify capture-affinity failures without touching a real desktop window."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from interview_copilot import window_capture as capture


class FakeUser32:
    def __init__(self, *, root=202, set_ok=True, get_ok=True, actual=0x11):
        self.GetAncestor = Mock(return_value=root)
        self.SetWindowDisplayAffinity = Mock(return_value=set_ok)

        def read_affinity(hwnd, output):
            if get_ok:
                ctypes.cast(output, ctypes.POINTER(wintypes.DWORD))[0] = actual
            return get_ok

        self.GetWindowDisplayAffinity = Mock(side_effect=read_affinity)


@contextmanager
def fake_windows(api, *, build=26100, os_name="nt"):
    version = Mock(return_value=SimpleNamespace(build=build))
    with patch.object(capture, "os", SimpleNamespace(name=os_name)), \
            patch.object(capture, "sys", SimpleNamespace(getwindowsversion=version)), \
            patch.object(capture.ctypes, "WinDLL", return_value=api, create=True) as load:
        yield load, version


class CapturePrivacyTests(unittest.TestCase):
    def test_enable_verifies_outer_window_on_first_supported_windows_build(self):
        api = FakeUser32(root=202, actual=0x11)
        with fake_windows(api, build=19041) as (load, _):
            self.assertEqual(capture.set_capture_excluded(101, True), 0x11)
        load.assert_called_once_with("user32", use_last_error=True)
        api.GetAncestor.assert_called_once_with(101, 2)
        api.SetWindowDisplayAffinity.assert_called_once_with(202, 0x11)
        self.assertEqual(api.GetWindowDisplayAffinity.call_args.args[0], 202)

    def test_disable_restores_none_even_on_old_windows(self):
        api = FakeUser32(actual=0)
        with fake_windows(api, build=18363):
            self.assertEqual(capture.set_capture_excluded(101, False), 0)
        api.SetWindowDisplayAffinity.assert_called_once_with(202, 0)
        api.GetWindowDisplayAffinity.assert_called_once()

    def test_old_windows_rejects_enable_before_setting_monitor_only_fallback(self):
        for build in (7601, 18363, 19040):
            with self.subTest(build=build):
                api = FakeUser32()
                with fake_windows(api, build=build) as (load, _):
                    with self.assertRaisesRegex(capture.CapturePrivacyError, "2004"):
                        capture.set_capture_excluded(101, True)
                load.assert_not_called()
                api.SetWindowDisplayAffinity.assert_not_called()

    def test_non_windows_rejects_without_calling_windows_version_or_apis(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                with fake_windows(FakeUser32(), os_name="posix") as (load, version):
                    with self.assertRaises(capture.CapturePrivacyError):
                        capture.set_capture_excluded(101, enabled)
                version.assert_not_called()
                load.assert_not_called()

    def test_missing_outer_handle_never_sets_affinity_on_widget_handle(self):
        for missing in (None, 0):
            with self.subTest(handle=missing):
                api = FakeUser32(root=missing)
                with fake_windows(api):
                    with self.assertRaisesRegex(capture.CapturePrivacyError, "取得窗口"):
                        capture.set_capture_excluded(101, True)
                api.SetWindowDisplayAffinity.assert_not_called()
                api.GetWindowDisplayAffinity.assert_not_called()

    def test_set_failure_is_not_reported_as_success_and_skips_readback(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                api = FakeUser32(set_ok=False)
                with fake_windows(api):
                    with self.assertRaisesRegex(capture.CapturePrivacyError, "未能应用"):
                        capture.set_capture_excluded(101, enabled)
                api.GetWindowDisplayAffinity.assert_not_called()

    def test_readback_failure_is_unconfirmed_even_after_successful_set(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                api = FakeUser32(get_ok=False)
                with fake_windows(api):
                    with self.assertRaisesRegex(capture.CapturePrivacyError, "未能确认"):
                        capture.set_capture_excluded(101, enabled)
                api.SetWindowDisplayAffinity.assert_called_once()
                api.GetWindowDisplayAffinity.assert_called_once()

    def test_monitor_only_or_unrestricted_readback_is_not_capture_exclusion(self):
        for actual in (0, 1, 0x12):
            with self.subTest(actual=actual):
                api = FakeUser32(actual=actual)
                with fake_windows(api):
                    with self.assertRaisesRegex(capture.CapturePrivacyError, "不一致"):
                        capture.set_capture_excluded(101, True)

    def test_disable_must_confirm_that_exclusion_was_removed(self):
        api = FakeUser32(actual=0x11)
        with fake_windows(api):
            with self.assertRaisesRegex(capture.CapturePrivacyError, "不一致"):
                capture.set_capture_excluded(101, False)

    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, "64-bit pointer regression")
    def test_native_ctypes_marshalling_preserves_full_child_and_outer_handles(self):
        # These are in-process callback addresses, not Windows calls. Start with
        # WinDLL's untyped/c_int defaults so missing prototypes cause truncation.
        widget, outer = 0x00000012ABCDEF01, 0x00000023ABCDEF02
        seen = []
        keep_alive = []

        def native_function(restype, argtypes, handler):
            callback = ctypes.CFUNCTYPE(restype, *argtypes)(handler)
            keep_alive.append(callback)
            address = ctypes.cast(callback, ctypes.c_void_p).value
            function = ctypes.CFUNCTYPE(ctypes.c_int)(address)
            function.argtypes = None
            return function

        def ancestor(hwnd, flags):
            seen.append(("ancestor", hwnd, flags))
            return outer

        def set_affinity(hwnd, affinity):
            seen.append(("set", hwnd, affinity))
            return 1

        def get_affinity(hwnd, output):
            seen.append(("get", hwnd))
            output[0] = 0x11
            return 1

        api = SimpleNamespace(
            GetAncestor=native_function(wintypes.HWND, [wintypes.HWND, wintypes.UINT], ancestor),
            SetWindowDisplayAffinity=native_function(
                wintypes.BOOL, [wintypes.HWND, wintypes.DWORD], set_affinity),
            GetWindowDisplayAffinity=native_function(
                wintypes.BOOL, [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], get_affinity),
        )
        with fake_windows(api):
            self.assertEqual(capture.set_capture_excluded(widget, True), 0x11)
        self.assertEqual(seen, [
            ("ancestor", widget, 2), ("set", outer, 0x11), ("get", outer),
        ])


if __name__ == "__main__":
    unittest.main()
