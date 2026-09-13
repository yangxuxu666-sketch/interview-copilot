from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, copy_metadata
APP = Path(SPECPATH).resolve().parents[1] / 'InterviewCopilot'
HERE = Path(SPECPATH)
datas = [(str(APP/'interview_copilot/static'), 'interview_copilot/static')]
datas += collect_data_files('docx')
datas += copy_metadata('dashscope')
datas += copy_metadata('PyAudioWPatch')
a = Analysis([str(APP/'main.py')], pathex=[str(APP)], binaries=[], datas=datas,
    hiddenimports=['interview_copilot.asr_worker','interview_copilot.overlay_window',
      'pyaudiowpatch','audioop','tkinter','tkinter.font',
      'dashscope.audio.qwen_omni.omni_realtime',
      'uvicorn.protocols.websockets.wsproto_impl','uvicorn.protocols.http.h11_impl','uvicorn.lifespan.on'],
    hookspath=[], hooksconfig={}, runtime_hooks=[str(HERE/'cloud_edition_hook.py')],
    excludes=['faster_whisper','ctranslate2','onnxruntime','av','numpy','scipy','tokenizers',
      'huggingface_hub','hf_xet','torch','torchaudio','tensorflow','pytest','_pytest',
      'setuptools','_distutils_hack','IPython','matplotlib'],
    noarchive=False, optimize=1)
pyz = PYZ(a.pure)
main = EXE(pyz, a.scripts, [], exclude_binaries=True, name='面试伴航',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False,
    icon=str(HERE/'app.ico'))
worker = EXE(pyz, a.scripts, [], exclude_binaries=True, name='InterviewCopilot-worker',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=True,
    icon=str(HERE/'app.ico'))
coll = COLLECT(main, worker, a.binaries, a.datas, strip=False, upx=False, name='InterviewCopilot-Windows')
