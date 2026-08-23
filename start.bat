@echo off
cd /d "%~dp0"
echo Installing packages if needed...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Python / pip is required.
  pause
  exit /b 1
)
start "" http://127.0.0.1:8791/
python server.py
if errorlevel 1 pause
