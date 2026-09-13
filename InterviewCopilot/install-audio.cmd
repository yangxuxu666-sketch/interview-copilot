@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt -r requirements-audio.txt
if errorlevel 1 goto failed
echo Audio dependencies installed. Open start.cmd, then prepare a model in Settings.
pause
exit /b 0
:failed
echo Installation failed. Check Python and network, then retry.
pause
exit /b 1
