@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Telegram Login - Market Muse
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. Run this from the project folder.
  echo Current: %CD%
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import telethon" 2>nul
if errorlevel 1 (
  echo Installing telethon...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
".venv\Scripts\python.exe" _muse_login.py
