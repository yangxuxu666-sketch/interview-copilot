"""Own one native answer window without importing a GUI into the web server."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from uuid import uuid4

from .runtime import child_command


class OverlayError(RuntimeError):
    pass


class OverlayProcess:
    def __init__(self):
        self.process = None
        self._lock = threading.Lock()

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    def open(self, port, token, data_dir):
        with self._lock:
            if self.running:
                return
            if sys.platform not in ("win32", "darwin"):
                raise OverlayError("独立悬浮窗目前支持 Windows 和 macOS。")
            ready = Path(data_dir) / "overlay-ready.json"
            ready.unlink(missing_ok=True)
            launch_id = uuid4().hex
            try:
                startup = None
                flags = 0
                if sys.platform == "win32":
                    startup = subprocess.STARTUPINFO()
                    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    startup.wShowWindow = 1
                    flags = subprocess.CREATE_NO_WINDOW
                self.process = subprocess.Popen(
                    child_command("--overlay-window"),
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=flags,
                    startupinfo=startup,
                )
                payload = {"port": port, "token": token, "data_dir": str(data_dir), "launch_id": launch_id}
                self.process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
                self.process.stdin.flush()
                # Report success only once Tk has actually mapped a window.
                import time
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline and self.running:
                    if ready.exists():
                        try:
                            signal = json.loads(ready.read_text(encoding="utf-8"))
                            # Windows venv launchers can start a child with a
                            # different PID. Match this launch, not the redirector.
                            if signal.get("launch_id") == launch_id and signal.get("mapped"):
                                return
                        except (ValueError, OSError):
                            pass
                    time.sleep(.05)
            except (OSError, ValueError):
                pass
            self._stop()
            raise OverlayError("悬浮窗未能打开。请确认 Python 安装包含 Tcl/Tk，然后重新启动工具。")

    def _stop(self):
        if self.running:
            try:
                self.process.stdin.write(b'{"op":"close"}\n')
                self.process.stdin.flush()
                self.process.stdin.close()
                self.process.wait(timeout=1.5)
                return
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            try:
                self.process.terminate()
                self.process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def close(self):
        with self._lock:
            self._stop()
