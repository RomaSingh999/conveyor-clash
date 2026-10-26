@echo off
cd /d "%~dp0"
title Conveyor Clash
if not exist ".venv\Scripts\python.exe" (
  echo Setting up for the first time. This takes about a minute...
  py -3 -m venv .venv 2>nul
  if not exist ".venv\Scripts\python.exe" python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo Python was not found. Install Python from python.org and tick Add Python to PATH.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo Install failed. Check your internet connection and run this file again.
  pause
  exit /b 1
)
echo.
echo Conveyor Clash is starting at http://localhost:8000
echo Keep this window open while you play. Close it to stop the game.
echo.
start "" cmd /c "timeout /t 3 /nobreak >nul & start http://localhost:8000"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
pause
