"""Collect licenses from a clean Mac build environment, without personal files."""
from __future__ import annotations
import hashlib
import importlib.metadata as metadata
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import sysconfig
import tarfile
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
import zipfile

NOTICE = re.compile(r"^(license|licence|copying|notice)([._-]|$)", re.I)
SKIP = {"pip", "setuptools", "wheel"}


def download(url, limit=32 * 1024 * 1024):
    """Read public release metadata/notices only; never execute downloaded code."""
    allowed = {"pypi.org", "files.pythonhosted.org", "raw.githubusercontent.com"}
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in allowed:
        raise RuntimeError("Unexpected license source URL: " + url)
    with urlopen(Request(url, headers={"User-Agent": "InterviewCopilot-license-collector"}), timeout=45) as response:
        final = urlparse(response.geturl())
        if final.scheme != "https" or final.hostname not in allowed:
            raise RuntimeError("Unexpected license source redirect")
        payload = response.read(limit + 1)
    if len(payload) > limit:
        raise RuntimeError("License source exceeded size limit: " + url)
    return payload


def source_notices(name, version):
    """Some PyObjC wheels omit license text; use their exact PyPI source release."""
    release = json.loads(download(f"https://pypi.org/pypi/{quote(name, safe='')}/{quote(version, safe='')}/json", 2 * 1024 * 1024))
    normalize = lambda value: re.sub(r"[-_.]+", "-", value).lower()
    if normalize(release["info"]["name"]) != normalize(name) or release["info"]["version"] != version:
        raise RuntimeError("PyPI returned a different package/version for " + name)
    sources = [entry for entry in release["urls"] if entry["packagetype"] == "sdist"]
    if len(sources) != 1:
        raise RuntimeError(f"Expected one official source release for {name} {version}")
    source = sources[0]
    payload = download(source["url"])
    digest = hashlib.sha256(payload).hexdigest()
    if digest != source["digests"]["sha256"]:
        raise RuntimeError(f"Source SHA-256 mismatch for {name} {version}")

    def notice_path(filename):
        path = PurePosixPath(filename)
        # Root notices and explicit licenses/ directories only. In particular,
        # PyObjC's copying.m unit-test source is not a copyright notice.
        return (not path.is_absolute() and ".." not in path.parts and "\\" not in filename
                and len(path.parts) > 1 and NOTICE.match(path.name)
                and (len(path.parts) == 2 or "licenses" in (part.lower() for part in path.parts[1:-1])))

    notices = []
    if zipfile.is_zipfile(BytesIO(payload)):
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            for entry in archive.infolist():
                if not entry.is_dir() and notice_path(entry.filename):
                    if entry.file_size > 1024 * 1024:
                        raise RuntimeError("Source license text exceeds size limit")
                    notices.append((entry.filename, archive.read(entry)))
    else:
        with tarfile.open(fileobj=BytesIO(payload), mode="r:*") as archive:
            for entry in archive:
                if entry.isfile() and notice_path(entry.name):
                    if entry.size > 1024 * 1024:
                        raise RuntimeError("Source license text exceeds size limit")
                    with archive.extractfile(entry) as stream:
                        notices.append((entry.name, stream.read()))
    if not notices:
        raise RuntimeError(f"No license text in official source release for {name} {version}")
    provenance = {"source_url": source["url"], "source_sha256": digest}
    return notices, provenance


def collect(target):
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    manifest = {"edition": "macOS cloud speech recognition", "packages": [], "runtime_components": []}

    def copy(source, relative):
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return {"path": relative.as_posix(), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}

    def write(payload, relative, **provenance):
        if not payload.strip():
            raise RuntimeError("License text is empty: " + str(relative))
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return {"path": relative.as_posix(), "sha256": hashlib.sha256(payload).hexdigest(), **provenance}

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
            print(f"Fetching exact-version source notices for {name} {distribution.version}…", flush=True)
            notices, provenance = source_notices(name, distribution.version)
            for filename, payload in notices:
                relative = Path(*PurePosixPath(filename).parts[1:])
                found.append(write(payload, Path("licenses") / (name + "-" + distribution.version) / relative,
                                   source_path=filename, **provenance))
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
    tcl_version = tk.eval("info patchlevel")
    tk_version = tk.eval("package present Tk")
    tk.destroy()
    # Python.org installers and standalone builds place notices either alongside
    # scripts or in their enclosing framework Resources directory.
    for component, folder, version in (("Tcl", tcl_dir, tcl_version), ("Tk", tk_dir, tk_version)):
        candidates = [folder / "license.terms", folder / "LICENSE", folder / "../Resources/license.terms"]
        if ".framework" in str(folder):
            framework = next((p for p in folder.parents if p.suffix == ".framework"), None)
            if framework:
                candidates += [framework / "Resources" / "license.terms", framework / "Resources" / "License.rtf"]
        license_file = next((p.resolve() for p in candidates if p.is_file()), None)
        if license_file is None:
            if not re.fullmatch(r"\d+\.\d+\.\d+", version):
                raise RuntimeError(f"Cannot resolve the official license for {component} runtime {version}")
            tag = "core-" + version.replace(".", "-")
            url = f"https://raw.githubusercontent.com/tcltk/{component.lower()}/{tag}/license.terms"
            print(f"Fetching {component} {version} license from its matching release tag…", flush=True)
            notice = write(download(url, 1024 * 1024), Path("licenses/runtime") / component / "license.terms",
                           source_url=url, source_version=version)
        else:
            notice = copy(license_file, Path("licenses/runtime") / component / license_file.name)
        manifest["runtime_components"].append({"name": component, "version": version, "files": [notice]})
    (target / "THIRD_PARTY_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (target / "THIRD_PARTY_NOTICES.txt").write_text(
        "This distribution includes third-party software. Complete license texts are in licenses/.\n"
        "The manifest lists installed build and runtime packages; build-only tools are included for notice completeness.\n"
        "PyInstaller's license includes its bootloader distribution exception.\n", encoding="utf-8")
    return manifest
