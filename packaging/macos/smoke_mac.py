"""Offline startup smoke check; no microphone, recording, Keychain write or API use."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--overlay", action="store_true", help="Also show and close the native overlay in a logged-in Mac desktop.")
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("Run this smoke check on macOS.")
    app = args.app.resolve()
    exe = app / "Contents/MacOS/面试伴航"
    worker = exe.with_name("InterviewCopilot-worker")
    if not exe.is_file() or not worker.is_file():
        raise RuntimeError("The app or its worker is missing.")
    # Both executables embed the same program. This protocol command must run
    # the ASR worker and exit; a second web server would be a packaging failure.
    child = subprocess.run([str(worker), "--asr-worker"], input=b'{"command":"packaging-invalid"}\n',
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
    if child.returncode != 1 or not child.stdout.strip().startswith(b"{"):
        raise RuntimeError("Frozen worker did not return its JSON failure protocol.")
    checks = ["Frozen helper dispatch and redirected JSON output"]
    with tempfile.TemporaryDirectory(prefix="interview-mac-smoke-") as directory:
        data = Path(directory)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        process = subprocess.Popen([str(exe), "--no-window", "--port", str(port), "--data-dir", str(data)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        client = httpx.Client(trust_env=False, timeout=5, headers={"Origin": base})
        try:
            for _ in range(150):
                if process.poll() is not None:
                    log = data / "startup-error.txt"
                    raise RuntimeError(log.read_text(encoding="utf-8") if log.exists() else "App exited before startup")
                try:
                    if client.get(base + "/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.2)
            else:
                raise RuntimeError("App startup timed out.")
            if client.get(base + "/api/state").status_code != 401:
                raise RuntimeError("Session authentication was not enforced.")
            instance = json.loads((data / "instance.json").read_text(encoding="utf-8"))
            client.cookies.set("interview_session", instance["token"])
            response = client.get(base + "/api/state")
            response.raise_for_status()
            state = response.json()
            assert state["capabilities"]["cloud_edition"]
            assert state["capabilities"]["platform"] == "macOS"
            assert state["capabilities"]["audio"]
            assert not state["capabilities"]["local_asr"]
            assert not state["capabilities"]["secret_error"]
            assert state["settings"]["asr_provider"] == "qwen"
            assert not state["settings"]["deepseek_key_set"] and not state["settings"]["cloud_asr_key_set"]
            assert state["session"]["answers"] == [] and state["session"]["transcripts"] == []
            for profile in state["profiles"]:
                assert all(not profile.get(field) for field in ("resume", "jd", "company", "materials", "company_notes", "custom_prompt", "source_notes"))
            checks.append("Authenticated launch with empty profiles, keys and history; cloud ASR default")
            device_response = client.get(base + "/api/audio/devices")
            device_response.raise_for_status()
            device_result = device_response.json()
            assert not device_result.get("error")
            assert len(device_result["devices"]) == 1 and device_result["devices"][0]["id"] == 0
            checks.append("ScreenCaptureKit/CoreAudio bindings load and expose the system mix without recording")
            for route in ("/", "/static/app.js", "/static/styles.css"):
                client.get(base + route).raise_for_status()
            result = client.post(base + "/api/import", files={"file": ("sample.txt", "Mac 上传检查".encode())})
            result.raise_for_status()
            assert "Mac 上传检查" in result.json()["text"]
            checks.append("Packaged web assets and UTF-8 text import")
            if args.overlay:
                result = client.post(base + "/api/overlay/open", json={})
                result.raise_for_status()
                assert result.json()["opened"]
                client.post(base + "/api/overlay/close").raise_for_status()
                checks.append("Native overlay opened and closed")
            duplicate = subprocess.run([str(exe), "--no-window", "--port", str(port), "--data-dir", str(data)],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            assert duplicate.returncode == 0
            assert json.loads((data / "instance.json").read_text())["pid"] == instance["pid"]
            checks.append("Repeated launch reuses the same instance")
        finally:
            try:
                client.post(base + "/api/shutdown")
            except httpx.HTTPError:
                pass
            client.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
        assert process.returncode == 0
        checks.append("Clean shutdown")
    args.report.write_text(json.dumps({"success": True, "checks": checks,
        "real_audio_verified": False, "keychain_write_verified": False,
        "note": "System-audio permissions, real transcription and Keychain writes require separate Mac acceptance."},
        ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
