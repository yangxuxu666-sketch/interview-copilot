"""Source/frozen paths and isolated child entry points for the desktop build."""
from __future__ import annotations

import atexit
from contextlib import contextmanager
import ctypes
import os
from pathlib import Path
import runpy
import sys
import threading


CHILD_MODULES = {
    "--asr-worker": "interview_copilot.asr_worker",
    "--overlay-window": "interview_copilot.overlay_window",
}
WORKER_EXECUTABLE = "InterviewCopilot-worker.exe"
_STANDARD_FALLBACKS = []
_EXTERNAL_LAUNCH_LOCK = threading.RLock()


@atexit.register
def _close_standard_fallbacks():
    for stream in _STANDARD_FALLBACKS:
        stream.close()


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def default_data_dir(source_root: Path) -> Path:
    """Keep source installs unchanged; installed builds use the current user."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "InterviewCopilot"
    if not is_frozen():
        return Path(source_root) / "data"
    local = os.environ.get("LOCALAPPDATA")
    parent = Path(local) if local else Path.home() / "AppData" / "Local"
    return parent / "InterviewCopilot"


def ensure_standard_streams():
    """Windowed EXEs lack streams, but console worker pipes must stay intact."""
    for name, mode in (("stdin", "r"), ("stdout", "w"), ("stderr", "w")):
        if getattr(sys, name) is None:
            stream = open(os.devnull, mode, encoding="utf-8")
            _STANDARD_FALLBACKS.append(stream)
            setattr(sys, name, stream)


def _external_path(path: str, bundle: Path) -> str:
    """Remove hook-added bundle directories without dropping system PATHs."""
    bundle = bundle.resolve()
    entries = []
    for entry in path.split(os.pathsep):
        try:
            bundled = bool(entry) and Path(os.path.expandvars(entry.strip('"'))).resolve().is_relative_to(bundle)
        except (OSError, ValueError):
            bundled = False
        if not bundled:
            entries.append(entry)
    return os.pathsep.join(entries)


@contextmanager
def external_browser_environment():
    """Keep PyInstaller DLL overrides out of system-installed browsers.

    Windows DLL search state is inherited by children. ShellExecute, used by
    webbrowser's fallback, also needs the temporary process PATH; Popen receives
    an explicit copy. Always restore both after the browser has been launched.
    Bundled workers deliberately do not use this context.
    """
    if is_frozen() and sys.platform == "darwin":
        # LaunchServices and installed browsers must not load bundled libraries.
        # Pass a copy to /usr/bin/open; the server and its workers keep theirs.
        child_env = dict(os.environ)
        for name in ("DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH", "DYLD_FALLBACK_LIBRARY_PATH"):
            if name + "_ORIG" in child_env:
                child_env[name] = child_env[name + "_ORIG"]
            else:
                child_env.pop(name, None)
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle and "PATH" in child_env:
            child_env["PATH"] = _external_path(child_env["PATH"], Path(bundle))
        yield child_env
        return
    if not is_frozen() or sys.platform != "win32":
        yield None
        return
    with _EXTERNAL_LAUNCH_LOCK:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_directory = kernel32.GetDllDirectoryW
        get_directory.argtypes = [ctypes.c_ulong, ctypes.c_wchar_p]
        get_directory.restype = ctypes.c_ulong
        set_directory = kernel32.SetDllDirectoryW
        set_directory.argtypes = [ctypes.c_wchar_p]
        set_directory.restype = ctypes.c_int
        size = get_directory(0, None)
        previous_directory = None
        if size:
            directory = ctypes.create_unicode_buffer(size)
            if not get_directory(size, directory):
                raise ctypes.WinError(ctypes.get_last_error())
            previous_directory = directory.value
        original_path = os.environ.get("PATH")
        child_env = dict(os.environ)
        bundle = getattr(sys, "_MEIPASS", None)
        if original_path is not None and bundle:
            child_env["PATH"] = _external_path(original_path, Path(bundle))
        if not set_directory(None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if original_path is not None:
                os.environ["PATH"] = child_env["PATH"]
            yield child_env
        finally:
            if original_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = original_path
            if not set_directory(previous_directory):
                raise ctypes.WinError(ctypes.get_last_error())


def child_command(flag: str) -> list[str]:
    if flag not in CHILD_MODULES:
        raise ValueError("Unknown desktop child entry point")
    if is_frozen():
        # A console-subsystem companion retains redirected binary pipes. The
        # parent uses CREATE_NO_WINDOW, so no terminal is shown to the user.
        worker = "InterviewCopilot-worker" if sys.platform == "darwin" else WORKER_EXECUTABLE
        return [str(Path(sys.executable).with_name(worker)), flag]
    filename = CHILD_MODULES[flag].rsplit(".", 1)[1] + ".py"
    return [sys.executable, "-u", str(Path(__file__).with_name(filename))]


def dispatch_child(argv=None) -> bool:
    """Dispatch before importing or creating the server; never launch it twice."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in CHILD_MODULES:
        return False
    if len(args) != 1:
        raise SystemExit("Desktop worker options do not accept extra arguments")
    runpy.run_module(CHILD_MODULES[args[0]], run_name="__main__")
    return True
