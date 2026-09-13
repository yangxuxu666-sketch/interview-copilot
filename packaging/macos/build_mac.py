"""Build on a real Mac and ZIP with ditto, preserving app symlinks and modes."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import uuid

from collect_notices import collect
from make_icon import generate

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=HERE.parents[1] / "InterviewCopilot")
    parser.add_argument("--output", type=Path, default=HERE.parents[1] / "mac-dist")
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("Mac .app 必须在 Mac 上构建；不能在 Windows 上生成可验证的 Mac 应用。")
    if sys.version_info[:2] != (3, 13):
        raise SystemExit("Please build with Python 3.13.")
    source, output = args.source.resolve(), args.output.resolve()
    if not (source / "main.py").is_file():
        raise SystemExit("Application source was not found.")
    output.mkdir(parents=True, exist_ok=True)
    staging = output / ("build-" + platform.machine() + "-" + uuid.uuid4().hex[:8])
    staging.mkdir()
    package = staging / ("面试伴航-Mac-" + platform.machine())
    package.mkdir()
    print("Collecting dependency notices…", flush=True)
    collect(package)
    if not (HERE / "app.icns").exists():
        generate(HERE / "app.icns")
    environment = dict(os.environ, INTERVIEW_SOURCE_ROOT=str(source), PYTHONNOUSERSITE="1")
    print("Building native app for " + platform.machine() + "…", flush=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--workpath", str(staging / "work"),
                    "--distpath", str(staging / "dist"), str(HERE / "InterviewCopilot-macos.spec")],
                   check=True, env=environment)
    app = staging / "dist" / "面试伴航.app"
    executable = app / "Contents" / "MacOS" / "面试伴航"
    worker = executable.with_name("InterviewCopilot-worker")
    if not executable.is_file() or not worker.is_file():
        raise RuntimeError("The Mac application or its helper was not produced.")
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    subprocess.run([sys.executable, str(HERE / "smoke_mac.py"), "--app", str(app),
                    "--report", str(package / "自动检查结果.json")], check=True)
    shutil.copytree(app, package / app.name, symlinks=True)
    license_file = source.parent / "LICENSE"
    if license_file.is_file():
        shutil.copyfile(license_file, package / "LICENSE.txt")
    shutil.copyfile(HERE / "独立应用使用说明.txt", package / "先看这里.txt")
    build_info = {"architecture": platform.machine(), "macOS_build_host": platform.mac_ver()[0],
                  "minimum_macos": ".".join(platform.mac_ver()[0].split(".")[:2]),
                  "python": platform.python_version(), "notarized": False, "real_audio_verified": False}
    (package / "构建信息.json").write_text(json.dumps(build_info, ensure_ascii=False, indent=2), encoding="utf-8")
    forbidden = {"secrets.dpapi", "state.json", "instance.json", "application.log", "startup-error.txt", ".env"}
    for path in package.rglob("*"):
        rel = path.relative_to(package)
        if path.name.lower() in forbidden or {".venv", "data", "browser-profile", ".git"}.intersection(rel.parts):
            raise RuntimeError(f"Private runtime file found in bundle: {rel}")
        if path.is_symlink() and not path.resolve().is_relative_to(package.resolve()):
            raise RuntimeError(f"Bundle symlink points outside the app: {rel}")
    destination = output / ("面试伴航-Mac-云端版-" + platform.machine() + ".zip")
    subprocess.run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(package), str(destination)], check=True)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix(".sha256.txt").write_text(digest + "  " + destination.name + "\n", encoding="utf-8")
    print(json.dumps({"archive": str(destination), "sha256": digest, "bytes": destination.stat().st_size,
                      **build_info}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
