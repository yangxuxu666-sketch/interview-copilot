"""Opt this process's native window out of supported Windows capture APIs."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import sys

WDA_NONE = 0
WDA_EXCLUDEFROMCAPTURE = 0x11


class CapturePrivacyError(RuntimeError):
    pass


def set_capture_excluded(widget_hwnd: int, enabled: bool) -> int:
    """Apply to the outer top-level HWND and return its verified affinity.

    This reports Windows policy, not proof of a meeting application's output.
    """
    if os.name != "nt" or (enabled and sys.getwindowsversion().build < 19041):
        raise CapturePrivacyError("需要 Windows 10 2004 或更新版本")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
    user32.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowDisplayAffinity.restype = wintypes.BOOL
    hwnd = user32.GetAncestor(widget_hwnd, 2)  # GA_ROOT: Tk's outer window.
    if not hwnd:
        raise CapturePrivacyError("暂时无法取得窗口，请重新打开小窗")
    desired = WDA_EXCLUDEFROMCAPTURE if enabled else WDA_NONE
    if not user32.SetWindowDisplayAffinity(hwnd, desired):
        raise CapturePrivacyError("Windows 未能应用此设置")
    actual = wintypes.DWORD()
    if not user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(actual)):
        raise CapturePrivacyError("Windows 未能确认此设置")
    if actual.value != desired:
        raise CapturePrivacyError("Windows 返回的设置与请求不一致")
    return actual.value
