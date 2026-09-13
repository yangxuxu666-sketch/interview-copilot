"""Build a relocatable Windows cloud edition from a clean virtual environment."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
from zipfile import ZIP_DEFLATED, ZipFile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "windows")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("Build the Windows application on Windows.")
    if sys.version_info[:2] != (3, 13):
        raise SystemExit("Build with Python 3.13 x64 and a clean virtual environment.")
    import struct
    if struct.calcsize("P") != 8:
        raise SystemExit("The Windows edition requires 64-bit Python.")
    output = args.output.resolve()
    staging = output / ("build-" + uuid.uuid4().hex[:8])
    staging.mkdir(parents=True)
    work = staging / "work"
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--workpath", str(work),
                    "--distpath", str(staging / "dist"), str(HERE / "InterviewCopilot.spec")], check=True)
    bundle = staging / "dist" / "InterviewCopilot-Windows"
    for name in ("面试伴航.exe", "InterviewCopilot-worker.exe"):
        if not (bundle / name).is_file():
            raise RuntimeError("The application or its helper was not produced.")
    notice_command = [sys.executable, str(HERE / "collect_notices.py"), str(bundle), "--quickstart"]
    for name in ("Analysis-00.toc", "PYZ-00.toc", "COLLECT-00.toc"):
        notice_command.extend(["--toc", str(work / "InterviewCopilot" / name)])
    subprocess.run(notice_command, check=True)
    shutil.copyfile(HERE / "创建桌面快捷方式.cmd", bundle / "创建桌面快捷方式.cmd")
    if (ROOT / "LICENSE").is_file():
        shutil.copyfile(ROOT / "LICENSE", bundle / "LICENSE.txt")
    files = sorted(p for p in bundle.rglob("*") if p.is_file())
    forbidden = {"secrets.dpapi", "state.json", "instance.json", "application.log", "startup-error.txt", ".env"}
    for path in files:
        rel = path.relative_to(bundle)
        if path.is_symlink() or path.name.lower() in forbidden or {"data", ".venv", "browser-profile", ".git"}.intersection(rel.parts):
            raise RuntimeError(f"Unexpected runtime data or symlink in bundle: {rel}")
    archive = output / "面试伴航-Windows-云端版.zip"
    with ZipFile(archive, "w", ZIP_DEFLATED, compresslevel=6) as zipped:
        for path in files:
            zipped.write(path, "面试伴航/" + path.relative_to(bundle).as_posix())
    with ZipFile(archive) as zipped:
        if zipped.testzip() is not None:
            raise RuntimeError("Archive integrity validation failed.")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".sha256.txt").write_text(digest + "  " + archive.name + "\n", encoding="utf-8")
    print(f"Built {archive} ({archive.stat().st_size:,} bytes)")
    print("Archive integrity passed. Test startup, uploads, audio and overlay on a target Windows PC before release.")


if __name__ == "__main__":
    main()
