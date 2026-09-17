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


def output_tail(value, limit=4000):
    """Keep runner diagnostics useful without dumping unbounded subprocess logs."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    value = value or ""
    return ("[truncated] " if len(value) > limit else "") + value[-limit:]


def startup_diagnostics(data, output):
    output.flush()
    output.seek(0, 2)
    output.seek(max(0, output.tell() - 12000))
    details = ["App output:\n" + output_tail(output.read())]
    log = data / "startup-error.txt"
    if log.is_file():
        with log.open("rb") as source:
            source.seek(0, 2)
            source.seek(max(0, source.tell() - 12000))
            details.append("startup-error.txt:\n" + output_tail(source.read()))
    return "\n".join(details)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


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
    try:
        child = subprocess.run([str(worker), "--asr-worker"], input=b'{"command":"packaging-invalid"}\n',
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Frozen worker timed out after 20 seconds.\nstdout:\n" +
                           output_tail(error.stdout) + "\nstderr:\n" + output_tail(error.stderr)) from error
    try:
        worker_reply = json.loads(child.stdout)
    except (ValueError, UnicodeDecodeError):
        worker_reply = None
    if child.returncode != 1 or worker_reply != {"ok": False, "error": "protocol"}:
        raise RuntimeError(f"Frozen worker failed its JSON protocol check (exit {child.returncode}).\n"
                           f"stdout:\n{output_tail(child.stdout)}\nstderr:\n{output_tail(child.stderr)}")
    checks = ["Frozen helper dispatch and redirected JSON output"]
    with tempfile.TemporaryDirectory(prefix="interview-mac-smoke-") as directory, tempfile.TemporaryFile() as output:
        data = Path(directory)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        process = subprocess.Popen([str(exe), "--no-window", "--port", str(port), "--data-dir", str(data)],
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
        client = httpx.Client(trust_env=False, timeout=5, headers={"Origin": base})
        stage = "application startup"
        try:
            health_status = "No health response received"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"App exited before startup (exit {process.returncode})")
                try:
                    health = client.get(base + "/health", timeout=1)
                    health_status = f"/health returned HTTP {health.status_code}"
                    if health.status_code == 200:
                        break
                except httpx.HTTPError as error:
                    health_status = f"/health: {type(error).__name__}: {error}"
                time.sleep(.2)
            else:
                raise RuntimeError("App startup timed out. " + health_status)
            stage = "session authentication and initial capabilities"
            if client.get(base + "/api/state").status_code != 401:
                raise RuntimeError("Session authentication was not enforced.")
            instance = json.loads((data / "instance.json").read_text(encoding="utf-8"))
            client.cookies.set("interview_session", instance["token"])
            response = client.get(base + "/api/state")
            response.raise_for_status()
            state = response.json()
            require(state["capabilities"]["cloud_edition"], "Cloud edition runtime hook was not applied")
            require(state["capabilities"]["platform"] == "macOS", "Application did not report the macOS platform")
            require(state["capabilities"]["audio"], "Packaged macOS system-audio imports are unavailable")
            require(not state["capabilities"]["local_asr"], "Cloud bundle unexpectedly enables local ASR")
            require(not state["capabilities"]["secret_error"], "Keychain initialization failed: " + str(state["capabilities"]["secret_error"]))
            require(state["settings"]["asr_provider"] == "qwen", "Fresh cloud bundle did not select Qwen ASR")
            require(not state["settings"]["deepseek_key_set"] and not state["settings"]["cloud_asr_key_set"], "Fresh bundle unexpectedly contains API keys")
            require(state["session"]["answers"] == [] and state["session"]["transcripts"] == [], "Fresh bundle unexpectedly contains session history")
            for profile in state["profiles"]:
                require(all(not profile.get(field) for field in ("resume", "jd", "company", "materials", "company_notes", "custom_prompt", "source_notes")), "Fresh bundle unexpectedly contains profile materials")
            checks.append("Authenticated launch with empty profiles, keys and history; cloud ASR default")
            stage = "macOS audio-framework import and device listing"
            device_response = client.get(base + "/api/audio/devices")
            device_response.raise_for_status()
            device_result = device_response.json()
            require(not device_result.get("error"), "Audio framework/device error: " + str(device_result.get("error")))
            require(len(device_result["devices"]) == 1 and device_result["devices"][0]["id"] == 0, "Expected one macOS system-mix device with id 0")
            checks.append("ScreenCaptureKit/CoreAudio bindings load and expose the system mix without recording")
            for route in ("/", "/static/app.js", "/static/styles.css"):
                stage = "packaged web resource " + route
                client.get(base + route).raise_for_status()
            stage = "UTF-8 text import"
            result = client.post(base + "/api/import", files={"file": ("sample.txt", "Mac 上传检查".encode())})
            result.raise_for_status()
            require("Mac 上传检查" in result.json()["text"], "Imported UTF-8 text was missing or corrupted")
            checks.append("Packaged web assets and UTF-8 text import")
            if args.overlay:
                stage = "native overlay (requires a logged-in Mac desktop)"
                result = client.post(base + "/api/overlay/open", json={})
                result.raise_for_status()
                require(result.json()["opened"], "Native overlay did not open; check for a logged-in desktop and bundled Tcl/Tk")
                client.post(base + "/api/overlay/close").raise_for_status()
                checks.append("Native overlay opened and closed")
            stage = "repeated launch"
            duplicate = subprocess.run([str(exe), "--no-window", "--port", str(port), "--data-dir", str(data)],
                                       stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, timeout=15)
            require(duplicate.returncode == 0, f"Repeated launch exited with {duplicate.returncode}")
            require(json.loads((data / "instance.json").read_text())["pid"] == instance["pid"], "Repeated launch replaced the original instance")
            checks.append("Repeated launch reuses the same instance")
        except Exception as error:
            raise RuntimeError(f"Mac smoke check failed during {stage}: {error}\n" + startup_diagnostics(data, output)) from error
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
        require(process.returncode == 0, f"Application shutdown exited with {process.returncode}\n" + startup_diagnostics(data, output))
        checks.append("Clean shutdown")
    args.report.write_text(json.dumps({"success": True, "checks": checks,
        "real_audio_verified": False, "keychain_write_verified": False, "overlay_verified": args.overlay,
        "note": "System-audio permissions, real transcription and Keychain writes require separate Mac acceptance."},
        ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
