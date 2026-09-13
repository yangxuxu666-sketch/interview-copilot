@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import fastapi,uvicorn,httpx,wsproto,multipart,pypdf,docx" >nul 2>nul
if errorlevel 1 (
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto failed
)
start "" ".venv\Scripts\pythonw.exe" "main.py" %*
exit /b 0
:failed
echo Installation failed. Check Python 3.11+ and your network, then retry.
pause
exit /b 1
