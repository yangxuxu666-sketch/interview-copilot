"""Run the local app in a dedicated Edge window, with no microphone access."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser

from interview_copilot.runtime import (default_data_dir, dispatch_child, ensure_standard_streams,
                                       external_browser_environment, is_frozen)

ROOT = Path(__file__).resolve().parent
STARTUP_DATA_DIR = None


def open_window(url, data_dir):
    if sys.platform == "darwin":
        with external_browser_environment() as environment:
            subprocess.Popen(["/usr/bin/open", url], env=environment,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    candidates = [Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
                  Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Microsoft/Edge/Application/msedge.exe"]
    edge = next((p for p in candidates if p.is_file()), None)
    if edge:
        startup = subprocess.STARTUPINFO() if os.name == "nt" else None
        if startup is not None:
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 1  # Windows SW_SHOWNORMAL; not exported by subprocess.
        with external_browser_environment() as environment:
            subprocess.Popen([str(edge), f"--app={url}", "--window-size=1440,940",
                              f"--user-data-dir={data_dir / 'browser-profile'}", "--no-first-run", "--no-default-browser-check"],
                             startupinfo=startup, env=environment)
    else:
        with external_browser_environment():
            webbrowser.open(url)


def main():
    global STARTUP_DATA_DIR
    ensure_standard_streams()
    parser = argparse.ArgumentParser(description="面试伴航本地应用")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir(ROOT))
    parser.add_argument("--no-window", action="store_true")
    args = parser.parse_args()
    data_dir = args.data_dir.resolve()
    STARTUP_DATA_DIR = data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    # Libraries that download models must stay under this app's data folder.
    os.environ.setdefault("HF_HOME", str(data_dir / "models" / "hf-cache"))
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("DO_NOT_TRACK", "1")
    import httpx
    import uvicorn
    from interview_copilot.app import create_app

    instance_path = data_dir / "instance.json"
    base_url = f"http://127.0.0.1:{args.port}"
    if instance_path.exists():
        try:
            instance = json.loads(instance_path.read_text(encoding="utf-8"))
            existing = f"http://127.0.0.1:{int(instance['port'])}"
            with httpx.Client(timeout=1, trust_env=False) as client:
                r = client.get(existing + "/api/state", cookies={"interview_session": instance["token"]})
            if r.status_code == 200:
                if not args.no_window:
                    open_window(existing + "/launch?token=" + instance["token"], data_dir)
                return
        except (ValueError, KeyError, OSError, httpx.HTTPError):
            pass
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        listener.bind(("127.0.0.1", args.port))
        listener.listen(128)
    except OSError:
        listener.close()
        raise RuntimeError(f"本机端口 {args.port} 已被占用。请关闭旧实例，或使用 --port 8766 启动。") from None
    token = secrets.token_urlsafe(32)
    app = create_app(data_dir, token, args.port)
    logging.basicConfig(filename=data_dir / "application.log", encoding="utf-8", level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_config=None,
                                           access_log=False, log_level="warning", ws="wsproto"))
    app.state.shutdown_callback = lambda: setattr(server, "should_exit", True)
    instance_path.write_text(json.dumps({"port": args.port, "token": token, "pid": os.getpid()}), encoding="utf-8")

    def show_when_ready():
        with httpx.Client(timeout=1, trust_env=False) as client:
            for _ in range(120):
                if server.should_exit:
                    return
                try:
                    if client.get(base_url + "/health").status_code == 200:
                        open_window(base_url + "/launch?token=" + token, data_dir)
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.25)
    if not args.no_window:
        threading.Thread(target=show_when_ready, daemon=True).start()
    try:
        server.run(sockets=[listener])
    finally:
        listener.close()
        if instance_path.exists():
            try:
                if json.loads(instance_path.read_text(encoding="utf-8")).get("token") == token:
                    instance_path.unlink()
            except (OSError, ValueError):
                pass


if __name__ == "__main__":
    # Private workers keep their real redirected streams and have their own
    # failure protocol. They must not open another server or a startup dialog.
    if not dispatch_child():
        try:
            main()
        except Exception as exc:
            log = (STARTUP_DATA_DIR or default_data_dir(ROOT)) / "startup-error.txt"
            advice = ("请确认已完整解压程序，并查看上述启动日志。" if is_frozen()
                      else "请重新运行 Mac 启动脚本查看或修复依赖。" if sys.platform == "darwin"
                      else "请运行 start.cmd 查看或修复依赖。")
            try:
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text(f"启动失败：{type(exc).__name__}: {exc}\n{advice}", encoding="utf-8")
            except OSError:
                pass
            if os.name == "nt":
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, f"启动失败：{exc}\n\n日志：{log}\n{advice}", "面试伴航", 0x10)
            raise
