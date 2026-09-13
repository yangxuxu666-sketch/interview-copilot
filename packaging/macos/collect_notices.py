"""Collect licenses from a clean Mac build environment, without personal files."""
from __future__ import annotations
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import re
import shutil
import sys
import sysconfig

NOTICE = re.compile(r"^(license|licence|copying|notice)([._-]|$)", re.I)
SKIP = {"pip", "setuptools", "wheel"}


def collect(target):
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    manifest = {"edition": "macOS cloud speech recognition", "packages": [], "runtime_components": []}

    def copy(source, relative):
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return {"path": relative.as_posix(), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}

    for distribution in sorted(metadata.distributions(), key=lambda d: d.metadata.get("Name", "")):
        name = distribution.metadata.get("Name", "")
        if not name or name.lower() in SKIP:
            continue
        found = []
        for relative in distribution.files or ():
            parts = relative.parts
            if not parts or ".." in parts or not parts[0].endswith(".dist-info"):
                continue
            if "licenses" not in (p.lower() for p in parts[1:-1]) and not NOTICE.match(parts[-1]):
                continue
            source = Path(distribution.locate_file(relative))
            if source.is_file():
                found.append(copy(source, Path("licenses") / (name + "-" + distribution.version) / Path(*parts[1:])))
        if not found:
            raise RuntimeError(f"No installed license text for {name} {distribution.version}; add its official license before sharing this build.")
        manifest["packages"].append({"name": name, "version": distribution.version, "files": found})

    python_candidates = [Path(sysconfig.get_path("stdlib")) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE.txt",
                         Path(sys.base_prefix) / "Resources" / "Python.app" / "Contents" / "Resources" / "LICENSE.txt"]
    python_license = next((p for p in python_candidates if p.is_file()), None)
    if python_license is None:
        raise RuntimeError("The installed Python LICENSE.txt is missing; install official Python 3.13 or add its official runtime license.")
    manifest["runtime_components"].append({"name": "Python " + sys.version.split()[0],
        "files": [copy(python_license, Path("licenses/runtime/Python/LICENSE.txt"))]})

    import tkinter
    tk = tkinter.Tk()
    tk.withdraw()
    tcl_dir = Path(tk.eval("info library"))
    tk_dir = Path(tk.eval("set tk_library"))
    tk.destroy()
    # Python.org installers and standalone builds place notices either alongside
    # scripts or in their enclosing framework Resources directory.
    for component, folder in (("Tcl", tcl_dir), ("Tk", tk_dir)):
        candidates = [folder / "license.terms", folder / "LICENSE", folder / "../Resources/license.terms"]
        if ".framework" in str(folder):
            framework = next((p for p in folder.parents if p.suffix == ".framework"), None)
            if framework:
                candidates += [framework / "Resources" / "license.terms", framework / "Resources" / "License.rtf"]
        license_file = next((p.resolve() for p in candidates if p.is_file()), None)
        if license_file is None:
            raise RuntimeError(f"The installed {component} license is missing. Add the license matching this runtime before sharing the app.")
        manifest["runtime_components"].append({"name": component,
            "files": [copy(license_file, Path("licenses/runtime") / component / license_file.name)]})
    (target / "THIRD_PARTY_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (target / "THIRD_PARTY_NOTICES.txt").write_text(
        "This distribution includes third-party software. Complete license texts are in licenses/.\n"
        "The manifest lists installed build and runtime packages; build-only tools are included for notice completeness.\n"
        "PyInstaller's license includes its bootloader distribution exception.\n", encoding="utf-8")
    return manifest
