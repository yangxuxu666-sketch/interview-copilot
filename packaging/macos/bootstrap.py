"""First-run setup on a Mac; never modify Apple's system Python or user data."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import platform
import subprocess
import sys
import venv


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("请在 Mac 上运行这个启动包。")
    if sys.version_info[:2] != (3, 13):
        raise SystemExit("请使用 Python 3.13（推荐 Python 官网的 macOS installer）。")
    if tuple(int(p) for p in platform.mac_ver()[0].split(".")[:2]) < (13, 0):
        raise SystemExit("系统声音监听需要 macOS 13 或更新版本。")
    try:
        import tkinter  # noqa: F401 — early, actionable check for Homebrew installs
    except ImportError:
        raise SystemExit("此 Python 缺少 Tk 小窗组件，请安装 Python 官网的 Python 3.13 macOS 版本。") from None

    data = Path.home() / "Library" / "Application Support" / "InterviewCopilot"
    data.mkdir(parents=True, exist_ok=True)
    runtime = data / ("runtime-macos-py313-" + platform.machine())
    python = runtime / "bin" / "python3"
    if not python.is_file():
        print("正在为面试伴航创建独立运行环境……", flush=True)
        venv.EnvBuilder(with_pip=True).create(runtime)
    requirements = ROOT / "InterviewCopilot" / "requirements-macos.txt"
    if not requirements.is_file():
        raise SystemExit("启动包不完整，请先全部解压。")
    marker = runtime / "interview-requirements.sha256"
    base_requirements = requirements.with_name("requirements.txt")
    digest = hashlib.sha256(requirements.read_bytes() + b"\x00" + base_requirements.read_bytes()).hexdigest()
    environment = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONNOUSERSITE="1")
    if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != digest:
        print("首次启动正在下载语音识别和界面组件，完成后会打开页面……", flush=True)
        subprocess.run([str(python), "-m", "pip", "install", "--only-binary=:all:",
                        "--index-url", "https://pypi.org/simple", "-r", str(requirements)],
                       check=True, env=environment)
        marker.write_text(digest + "\n", encoding="utf-8")
    if args.build:
        subprocess.run([str(python), "-m", "pip", "install", "--only-binary=:all:",
                        "--index-url", "https://pypi.org/simple", "-r", str(HERE / "requirements-build.txt")],
                       check=True, env=environment)
        subprocess.run([str(python), str(HERE / "build_mac.py"), "--source", str(ROOT / "InterviewCopilot"),
                        "--output", str(ROOT / "mac-dist")], check=True, env=environment)
        subprocess.run(["/usr/bin/open", str(ROOT / "mac-dist")], check=True)
        return
    print("正在启动。使用时请保留这个终端窗口；退出请使用应用中的退出按钮。", flush=True)
    os.execve(str(python), [str(python), str(HERE / "run_cloud.py"),
                          "--data-dir", str(data)], environment)


if __name__ == "__main__":
    main()
