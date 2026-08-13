@echo off
cd /d "%~dp0"
title SKSQ Valuation Agent
echo ==================================================
echo    SKSQ Valuation Agent
echo    A browser window will open shortly...
echo    (Close this window to STOP the server)
echo ==================================================
echo.
if not exist ".venv\Scripts\streamlit.exe" (
  echo [ERROR] .venv not found. Run this from the project folder.
  echo Current: %CD%
  pause
  exit /b 1
)
".venv\Scripts\streamlit.exe" run "app.py"
echo.
echo Server stopped. Press any key to close.
pause >nul