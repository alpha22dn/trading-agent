@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat (
  echo Run setup.bat first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
title Trading Agent
rem Opens the dashboard a few seconds after the server starts. Keep this window open while trading.
start "" /b cmd /c "timeout /t 4 /nobreak >nul & start http://127.0.0.1:8000"
python -m agent.cli serve
pause
