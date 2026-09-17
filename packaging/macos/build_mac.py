"""Build a native Mac app, ZIP archive, and drag-to-Applications DMG on macOS."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import uuid

from collect_notices import collect
from make_icon import generate

HERE = Path(__file__).resolve().parent


def write_digest(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(path.suffix + ".sha256.txt").write_text(
        digest + "  " + path.name + "\n", encoding="utf-8"
    )
    return digest


def make_dmg(app: Path, package: Path, destination: Path) -> None:
    """Create a Finder-friendly image without putting runtime data in it."""
    root = package.parent / "dmg-root"
    root.mkdir()
    shutil.copytree(app, root / app.name, symlinks=True)
    os.symlink("/Applications", root / "Applications")
    for name in ("先看这里.txt", "LICENSE.txt", "THIRD_PARTY_NOTICES.txt", "THIRD_PARTY_MANIFEST.json", "构建信息.json"):
        source = package / name
        if source.is_file():
            shutil.copyfile(source, root / name)
    licenses = package / "licenses"
    if licenses.is_dir():
        shutil.copytree(licenses, root / "licenses", symlinks=True)
    subprocess.run([
        "/usr/bin/hdiutil", "create", "-ov", "-format", "UDZO", "-volname", "面试伴航",
        "-srcfolder", str(root), str(destination),
    ], check=True)


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
    with (app / "Contents" / "Info.plist").open("rb") as source_plist:
        app_info = plistlib.load(source_plist)
    if app_info.get("LSBackgroundOnly") is not False:
        raise RuntimeError("The app is marked background-only; the native answer window requires GUI access.")
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    # collect() already opened a Tk root in this desktop session. Also verify
    # the frozen companion's Tcl/Tk libraries and window-open/close protocol.
    subprocess.run([sys.executable, str(HERE / "smoke_mac.py"), "--app", str(app),
                    "--report", str(package / "自动检查结果.json"), "--overlay"], check=True)
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
    digest = write_digest(destination)
    dmg = output / ("面试伴航-Mac-云端版-" + platform.machine() + ".dmg")
    make_dmg(app, package, dmg)
    dmg_digest = write_digest(dmg)
    print(json.dumps({
        "archive": str(destination), "sha256": digest, "bytes": destination.stat().st_size,
        "dmg": str(dmg), "dmg_sha256": dmg_digest, "dmg_bytes": dmg.stat().st_size,
        **build_info,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
