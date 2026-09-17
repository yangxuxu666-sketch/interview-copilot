from pathlib import Path
import os
import platform
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

if sys.platform != "darwin":
    raise RuntimeError("Mac applications must be built on macOS.")
HERE = Path(SPECPATH)
APP = Path(os.environ["INTERVIEW_SOURCE_ROOT"])
ARCH = platform.machine()
datas = [(str(APP / "interview_copilot/static"), "interview_copilot/static")]
datas += collect_data_files("docx") + copy_metadata("dashscope")
datas += copy_metadata("keyring")
hiddenimports = [
    "interview_copilot.asr_worker", "interview_copilot.overlay_window",
    "audioop", "tkinter", "tkinter.font", "keyring.backends.macOS",
    "ScreenCaptureKit", "Foundation", "CoreAudio", "CoreMedia", "Quartz", "dispatch", "objc",
    "dashscope.audio.qwen_omni.omni_realtime",
    "uvicorn.protocols.websockets.wsproto_impl", "uvicorn.protocols.http.h11_impl", "uvicorn.lifespan.on",
]
for framework in ("ScreenCaptureKit", "Foundation", "CoreAudio", "CoreMedia", "Quartz", "dispatch"):
    hiddenimports += collect_submodules(framework)
a = Analysis([str(APP / "main.py")], pathex=[str(APP)], binaries=[], datas=datas,
    hiddenimports=hiddenimports, hookspath=[], hooksconfig={},
    runtime_hooks=[str(HERE / "cloud_edition_hook.py")],
    excludes=["pyaudiowpatch", "faster_whisper", "ctranslate2", "onnxruntime", "av", "numpy", "scipy",
      "tokenizers", "huggingface_hub", "hf_xet", "torch", "torchaudio", "tensorflow", "pytest", "_pytest",
      "setuptools", "_distutils_hack", "IPython", "matplotlib"],
    noarchive=False, optimize=1)
pyz = PYZ(a.pure)
main = EXE(pyz, a.scripts, [], exclude_binaries=True, name="面试伴航", console=False,
    debug=False, strip=False, upx=False, argv_emulation=False, target_arch=ARCH,
    codesign_identity=None, entitlements_file=None)
worker = EXE(pyz, a.scripts, [], exclude_binaries=True, name="InterviewCopilot-worker", console=True,
    debug=False, strip=False, upx=False, argv_emulation=False, target_arch=ARCH,
    codesign_identity=None, entitlements_file=None)
coll = COLLECT(main, worker, a.binaries, a.datas, strip=False, upx=False, name="InterviewCopilot-macos")
app = BUNDLE(coll, name="面试伴航.app", icon=str(HERE / "app.icns"),
    bundle_identifier="local.interviewcopilot.desktop", version="1.0.0",
    info_plist={
        "CFBundleDisplayName": "面试伴航",
        "CFBundleShortVersionString": "1.0.0",
        "LSMinimumSystemVersion": ".".join(platform.mac_ver()[0].split(".")[:2]),
        # COLLECT inherits console=True from the companion worker. Explicitly
        # retain GUI access for the native Tk answer window in this bundle.
        "LSBackgroundOnly": False,
        "NSHighResolutionCapable": True,
        "NSScreenCaptureUsageDescription": "监听面试软件播放的系统声音并发送给你配置的语音识别服务；不采集麦克风。",
        "NSAudioCaptureUsageDescription": "将电脑播放的面试官声音转为文字；不采集麦克风。",
    })
