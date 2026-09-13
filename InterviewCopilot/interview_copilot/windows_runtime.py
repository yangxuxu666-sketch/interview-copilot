"""Load a current Microsoft C++ runtime privately for the ASR worker.

The system runtime is never replaced. A newer, signed copy already installed
with Microsoft Edge may be copied into the application's own data directory.
That copy survives Edge updates and is excluded from source distributions.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

_runtime_handle = None
_runtime_path = None
_MINIMUM = (14, 50, 0, 0)  # Conservative minimum verified with this app's native libraries.


def _file_version(path: Path) -> tuple[int, ...]:
    from ctypes import wintypes

    dll = ctypes.WinDLL("version.dll", use_last_error=True)
    dll.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    dll.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    dll.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    dll.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                 ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    size = dll.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return ()
    buffer = ctypes.create_string_buffer(size)
    if not dll.GetFileVersionInfoW(str(path), 0, size, buffer):
        return ()
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if not dll.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        return ()
    words = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
    if length.value < 16 or words[0] != 0xFEEF04BD:
        return ()
    return (words[2] >> 16, words[2] & 0xFFFF, words[3] >> 16, words[3] & 0xFFFF)


def _is_microsoft_signed(path: Path) -> bool:
    # A structured environment value avoids quoting a filesystem path as code.
    env = dict(os.environ, INTERVIEW_RUNTIME_CANDIDATE=str(path))
    powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    # A launcher running under PowerShell 7 may export its incompatible module
    # directories. This subprocess uses the modules matching Windows PowerShell.
    env["PSModulePath"] = str(powershell.parent / "Modules")
    script = ("$s = Get-AuthenticodeSignature -LiteralPath $env:INTERVIEW_RUNTIME_CANDIDATE; "
              "if ($s.Status -eq 'Valid' -and $s.SignerCertificate.Subject -match "
              r"'(?:^|,\s*)O=Microsoft Corporation(?:,|$)') { exit 0 }; exit 1")
    try:
        result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
                                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def prepare_windows_runtime(data_dir: Path | None = None) -> Path | None:
    """Call only in the isolated worker, before importing any inference library."""
    global _runtime_handle, _runtime_path
    if os.name != "nt":
        return None
    if _runtime_handle is not None:
        return _runtime_path
    data_dir = Path(data_dir) if data_dir else Path(__file__).resolve().parents[1] / "data"
    private = data_dir / "runtime" / "win-x64" / "msvcp140.dll"
    system = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/msvcp140.dll"
    candidates = [private, system]
    for variable, fallback in (("PROGRAMFILES(X86)", "C:/Program Files (x86)"),
                               ("PROGRAMFILES", "C:/Program Files")):
        edge = Path(os.environ.get(variable, fallback)) / "Microsoft/Edge/Application"
        candidates.extend(sorted(edge.glob("[0-9]*/msvcp140.dll"), key=_file_version, reverse=True))
    source = next((p for p in candidates
                   if p.is_file() and _file_version(p) >= _MINIMUM and _is_microsoft_signed(p)), None)
    if source is None:
        raise RuntimeError("本地语音需要新版 Microsoft Visual C++ x64 运行组件。"
                           "请安装微软最新版运行库后重试：https://aka.ms/vc14/vc_redist.x64.exe")
    if source not in (private, system):
        private.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="runtime-", suffix=".dll", dir=private.parent)
        os.close(fd)
        try:
            shutil.copyfile(source, temporary)
            os.replace(temporary, private)
        finally:
            Path(temporary).unlink(missing_ok=True)
        source = private
    _runtime_handle = ctypes.WinDLL(str(source))
    _runtime_path = source
    return source
