@echo off
cd /d "%~dp0"
title The Associate
echo ==================================================
echo    The Associate
echo    Open http://localhost:8501 in your browser
echo    (Close this window to STOP the server)
echo ==================================================
echo.
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. Run this from the project folder.
  echo Current: %CD%
  pause
  exit /b 1
)
start "" http://localhost:8501
".venv\Scripts\python.exe" -m uvicorn server.main:app --port 8501
echo.
echo Server stopped. Press any key to close.
pause >nul
