@echo off
cd /d "%~dp0"
start "" http://127.0.0.1:8791/
python server.py
if errorlevel 1 pause
