"""Copy selected installed dependency notices without copying code or user data.

Run with the app virtual environment's Python. Optional --toc inputs identify
all installed distributions owning files in PyInstaller TOCs, including
optional imports outside the declared runtime dependency closure. This script
uses installed wheel metadata, never imports the dependencies.
"""
from __future__ import annotations

import argparse
import ast
from collections import deque
from hashlib import sha256
import importlib.metadata as metadata
import json
from pathlib import Path
import re
import shutil
import sys
import sysconfig

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOTS = (
    "fastapi", "uvicorn", "wsproto", "httpx", "python-multipart", "pypdf",
    "python-docx", "PyAudioWPatch", "dashscope", "audioop-lts",
)
EXCLUDED = {
    "av", "numpy", "scipy", "onnxruntime", "ctranslate2", "faster-whisper",
    "huggingface-hub", "hf-xet", "tokenizers", "torch", "transformers",
    "pytest", "pip", "pyinstaller", "pyinstaller-hooks-contrib", "pefile",
    "altgraph", "pywin32-ctypes", "setuptools",
}
# PyInstaller's own bootstrap/runtime-hook notices are handled separately.
# Any other excluded distribution actually present in a TOC is a packaging
# error, rather than something whose missing notice should be silently ignored.
BOOTSTRAP_DISTRIBUTIONS = {"pyinstaller", "pyinstaller-hooks-contrib"}
NOTICE_NAME = re.compile(r"^(?:licen[sc]e|copying|notice)(?:[._-]|$)", re.I)


def normalized(path):
    return str(Path(path).resolve()).casefold()


def dependency_closure(distributions, roots):
    """Evaluate the current platform and explicitly requested extras only."""
    environment = default_environment()
    selected, requested, visited = {}, {}, {}
    pending = deque(Requirement(root) for root in roots)
    while pending:
        requirement = pending.popleft()
        name = canonicalize_name(requirement.name)
        if name in EXCLUDED:
            raise RuntimeError(f"Cloud edition unexpectedly requires excluded package: {name}")
        distribution = distributions.get(name)
        if distribution is None:
            raise RuntimeError(f"Missing installed distribution: {name}")
        if requirement.specifier and not requirement.specifier.contains(distribution.version):
            raise RuntimeError(f"Installed {name} {distribution.version} does not satisfy {requirement.specifier}")
        extras = requested.setdefault(name, {""})
        extras.update(requirement.extras)
        if visited.get(name) == extras:
            continue
        visited[name] = set(extras)
        selected[name] = distribution
        for text in distribution.requires or ():
            child = Requirement(text)
            if child.marker is None or any(
                child.marker.evaluate({**environment, "extra": extra}) for extra in extras
            ):
                pending.append(child)
    return selected


def toc_source_paths(paths):
    result = set()
    def visit(value):
        if isinstance(value, (tuple, list)):
            # A TOC record has (destination/module name, source path, type).
            if len(value) == 3 and isinstance(value[1], str) and isinstance(value[2], str):
                candidate = Path(value[1])
                if candidate.is_absolute():
                    result.add(normalized(candidate))
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)
    for path in paths:
        visit(ast.literal_eval(path.read_text(encoding="utf-8")))
    return result


def represented_in_toc(distribution, sources):
    return any(normalized(distribution.locate_file(file)) in sources
               for file in distribution.files or ())


def validate_bootstrap_files(name, distribution, sources):
    allowed = {
        "pyinstaller": ("PyInstaller/loader/", "PyInstaller/bootloader/",
                        "PyInstaller/hooks/rthooks/", "PyInstaller/fake-modules/"),
        "pyinstaller-hooks-contrib": ("_pyinstaller_hooks_contrib/rthooks/",),
    }[name]
    for relative in distribution.files or ():
        if normalized(distribution.locate_file(relative)) not in sources:
            continue
        path = relative.as_posix()
        if path.startswith(allowed) or (relative.parts and relative.parts[0].endswith(".dist-info")):
            continue
        raise RuntimeError(f"Build-only {name} file is actually bundled: {path}")


def license_paths(distribution):
    """Select only wheel-owned dist-info notice files, including vendor texts."""
    result = []
    for relative in distribution.files or ():
        parts = relative.parts
        if not parts or not parts[0].endswith(".dist-info") or ".." in parts:
            continue
        if "licenses" in (part.lower() for part in parts[1:-1]) or NOTICE_NAME.match(parts[-1]):
            source = Path(distribution.locate_file(relative))
            if source.is_file():
                result.append((source, Path(*parts[1:])))
    return sorted(result, key=lambda pair: pair[1].as_posix())


def copy_notice(source, destination, output, provenance):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return {"path": destination.relative_to(output).as_posix(),
            "sha256": sha256(destination.read_bytes()).hexdigest(),
            "source": provenance}


def license_label(distribution):
    meta = distribution.metadata
    value = meta.get("License-Expression") or meta.get("License") or ""
    if value and len(value) < 120 and "\n" not in value:
        return value
    classifiers = [part.split(" :: ")[-1] for part in meta.get_all("Classifier", [])
                   if part.startswith("License ::")]
    return ", ".join(classifiers) or "See bundled license text"


def generate(args):
    site = args.site_packages.resolve()
    output = args.target.resolve()
    installed = list(metadata.distributions(path=[str(site)]))
    build_distributions = list(metadata.distributions(path=[str(args.build_tools.resolve())]))
    distributions = {
        canonicalize_name(dist.metadata["Name"]): dist
        for dist in installed
        if dist.metadata.get("Name")
    }
    selected = dependency_closure(distributions, args.root or ROOTS)
    dependency_names = sorted(selected)
    bootstrap_bundled = {}
    selection = "runtime dependency closure; optional development/test extras excluded"
    if args.toc:
        sources = toc_source_paths(args.toc)
        if not sources:
            raise RuntimeError("No source paths found in supplied PyInstaller TOCs")
        selected = {}
        for dist in installed + build_distributions:
            name = canonicalize_name(dist.metadata.get("Name", ""))
            if not name or not represented_in_toc(dist, sources):
                continue
            if name in BOOTSTRAP_DISTRIBUTIONS:
                validate_bootstrap_files(name, dist, sources)
                bootstrap_bundled[name] = dist
                continue
            if name in EXCLUDED:
                raise RuntimeError(f"Excluded distribution is actually bundled: {name} {dist.version}")
            if name in selected and selected[name].version != dist.version:
                raise RuntimeError(f"Multiple bundled versions found for {name}; notices need separate version entries")
            # Match the distribution that owns the actual TOC source paths.
            # Optional imports such as packaging/PyYAML may be absent from the
            # project's declared runtime dependency tree but are still bundled.
            selected[name] = dist
        selection = "all installed distributions owning source files in supplied PyInstaller TOCs"
    if args.bundled_distributions:
        raw = args.bundled_distributions.read_text(encoding="utf-8")
        names = json.loads(raw) if raw.lstrip().startswith("[") else raw.splitlines()
        allowed = {canonicalize_name(name.strip()) for name in names if name.strip() and not name.lstrip().startswith("#")}
        unknown = allowed - (set(distributions) | set(selected))
        if unknown:
            raise RuntimeError(f"Requested bundled distributions are not installed: {sorted(unknown)}")
        if allowed & EXCLUDED:
            raise RuntimeError(f"Cloud edition excluded packages requested: {sorted(allowed & EXCLUDED)}")
        selected = {name: dist for name, dist in selected.items() if name in allowed}
        selection += "; restricted by explicit bundled-distribution list"
    if not selected:
        raise RuntimeError("No bundled distributions selected")
    # Never recursively delete an existing target: a packaging directory can
    # already contain the executable. Only write this helper's named files.
    output.mkdir(parents=True, exist_ok=True)
    manifest = {"edition": "Windows x64 cloud speech recognition",
                "selection": selection, "dependency_roots": list(args.root or ROOTS),
                "dependency_closure": dependency_names,
                "excluded_by_bundle_filter": sorted(set(dependency_names) - set(selected)),
                "additional_bundled_distributions": sorted(set(selected) - set(dependency_names)),
                "packages": [], "runtime_components": []}
    for name, dist in sorted(selected.items()):
        paths = license_paths(dist)
        if not paths:
            raise RuntimeError(f"No installed license text found for {name} {dist.version}")
        entry = {"name": dist.metadata["Name"], "version": dist.version,
                 "license": license_label(dist), "files": []}
        for source, relative in paths:
            destination = output / "licenses" / f"{name}-{dist.version}" / relative
            entry["files"].append(copy_notice(source, destination, output,
                f"installed wheel {name} {dist.version}: {relative.as_posix()}"))
        # The fork's dist-info includes Apache terms. Preserve the original
        # PyAudio MIT notice carried in the package source as well.
        if name == "pyaudiowpatch":
            source = site / "pyaudiowpatch" / "__init__.py"
            lines = source.read_text(encoding="utf-8").split("# PyAudioWPatch :", 1)[0].splitlines()
            text = "\n".join(line.removeprefix("# ").removeprefix("#") for line in lines).strip() + "\n"
            destination = output / "licenses" / f"{name}-{dist.version}" / "PyAudio-MIT.txt"
            destination.write_text(text, encoding="utf-8")
            entry["files"].append({"path": destination.relative_to(output).as_posix(),
                "sha256": sha256(destination.read_bytes()).hexdigest(),
                "source": "Original PyAudio notice in installed pyaudiowpatch/__init__.py"})
        manifest["packages"].append(entry)

    python_home = args.python_home.resolve()
    python_license = python_home / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("Python runtime LICENSE.txt was not found")
    runtime_files = [("Python", python_license, "Python/LICENSE.txt")]
    for component in ("tcl", "tk"):
        for source in sorted((python_home / "tcl").glob(component + "[0-9]*/license.terms")):
            runtime_files.append((source.parent.name, source, f"{source.parent.name}/license.terms"))
    for name, source, relative in runtime_files:
        manifest["runtime_components"].append({"name": name,
            "files": [copy_notice(source, output / "licenses" / "runtime" / relative,
                                  output, f"installed Python runtime: {relative}")]})
    pyinstaller = next((dist for dist in build_distributions
                        if canonicalize_name(dist.metadata.get("Name", "")) == "pyinstaller"), None)
    if pyinstaller is None:
        raise RuntimeError("PyInstaller build-tools distribution was not found")
    bootloader_paths = license_paths(pyinstaller)
    if not bootloader_paths:
        raise RuntimeError("PyInstaller bootloader exception text was not found")
    component = {"name": "PyInstaller bootloader and runtime hooks",
                 "version": pyinstaller.version, "files": []}
    for source, relative in bootloader_paths:
        component["files"].append(copy_notice(source,
            output / "licenses" / "runtime" / "PyInstaller" / relative, output,
            f"PyInstaller {pyinstaller.version} installed licensing text (includes bootloader exception)"))
    manifest["runtime_components"].append(component)
    community_hooks = bootstrap_bundled.get("pyinstaller-hooks-contrib")
    if community_hooks is not None:
        paths = license_paths(community_hooks)
        if not paths:
            raise RuntimeError("PyInstaller Community runtime-hook license text was not found")
        component = {"name": "PyInstaller Community runtime hooks (Apache-2.0)",
                     "version": community_hooks.version, "files": []}
        for source, relative in paths:
            component["files"].append(copy_notice(source,
                output / "licenses" / "runtime" / "PyInstaller-Community-Hooks" / relative,
                output, f"PyInstaller Community Hooks {community_hooks.version}; only Apache-2.0 runtime hooks are bundled"))
        manifest["runtime_components"].append(component)
    # Explicit supplementary notices (e.g. Tcl source distribution terms).
    supplemental = Path(__file__).parent / "supplemental-notices"
    if supplemental.is_dir():
        files = []
        for source in sorted(supplemental.iterdir()):
            if source.is_file() and source.suffix in {".txt", ".md", ".terms"}:
                files.append(copy_notice(source, output / "licenses" / "supplemental" / source.name,
                                         output, f"packaging supplemental notice: {source.name}"))
        if files:
            manifest["runtime_components"].append({"name": "Additional bundled component notices", "files": files})
    manifest_path = output / "THIRD_PARTY_MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows = ["# Third-party software notices", "",
            "面试伴航 Windows x64 云端识别版包含下列第三方组件。版权和许可归各自权利人所有。",
            "完整许可与版权声明见 `licenses/`；逐文件校验信息见 `THIRD_PARTY_MANIFEST.json`。",
            "本清单不授予任何第三方商标、API 服务或账户权限。语音和回答服务需使用者自行配置 API Key。", "",
            "| Component | Version | License / notice |", "|---|---|---|"]
    for entry in manifest["packages"]:
        link = next((file["path"] for file in entry["files"]
                     if re.match(r"^licen[sc]e(?:[._-]|$)", Path(file["path"]).name, re.I)),
                    entry["files"][0]["path"])
        label = entry["license"].replace("|", "\\|")
        rows.append(f"| {entry['name']} | {entry['version']} | [{label}]({link}) |")
    rows.extend(["", "## Runtime components", ""])
    for entry in manifest["runtime_components"]:
        links = ", ".join(f"[{Path(file['path']).name}]({file['path']})" for file in entry["files"])
        rows.append(f"- {entry['name']}: {links}")
    rows.extend(["", "此分享包不包含本地 Whisper 模型、FFmpeg/PyAV、CTranslate2、ONNX Runtime，",
                 "也不包含原使用者的简历、历史问答、浏览器资料或 API 密钥。", ""])
    (output / "THIRD_PARTY_NOTICES.md").write_text("\n".join(rows), encoding="utf-8")
    if args.quickstart:
        shutil.copyfile(Path(__file__).with_name("朋友使用说明.txt"), output / "先看这里.txt")
    print(json.dumps({"packages": len(manifest["packages"]),
                      "runtime_components": len(manifest["runtime_components"]),
                      "filtered_out": manifest["excluded_by_bundle_filter"]}, ensure_ascii=False))


def main():
    workspace = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="Output directory receiving only notices and optional quickstart")
    parser.add_argument("--site-packages", type=Path,
                        default=Path(sysconfig.get_path("purelib")))
    parser.add_argument("--python-home", type=Path, default=Path(sys.base_prefix))
    tool_root = Path(sysconfig.get_path("purelib"))
    parser.add_argument("--tool-root", "--build-tools", dest="build_tools", type=Path,
                        default=tool_root, help="PyInstaller installed tools directory (default: current environment site-packages)")
    parser.add_argument("--root", action="append", help="Override root distributions; no extras are enabled implicitly")
    parser.add_argument("--toc", type=Path, action="append", help="PyInstaller Analysis/PYZ/COLLECT TOC; repeatable")
    parser.add_argument("--bundled-distributions", type=Path, help="Optional JSON array or newline list of bundled distribution names")
    parser.add_argument("--quickstart", action="store_true")
    generate(parser.parse_args())


if __name__ == "__main__":
    main()
