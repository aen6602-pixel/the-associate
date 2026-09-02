@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title The Associate - 최신 코드 받기

echo ==================================================
echo    최신 코드 받기 (다른 PC에서 한 작업 가져오기)
echo ==================================================
echo.

git --version >nul 2>&1
if errorlevel 1 goto nogit

echo 아직 저장하지 않은 내 변경사항 확인 중...
git diff --quiet
if errorlevel 1 goto dirty
git diff --cached --quiet
if errorlevel 1 goto dirty

echo   깨끗함. 받아옵니다.
echo.
git pull --ff-only origin main
if errorlevel 1 goto conflict

echo.
echo 패키지 최신화 중...
if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
echo.
echo ==================================================
echo    완료! SKSQ_실행.bat 으로 앱을 켜세요.
echo ==================================================
goto end

:dirty
echo.
echo   [멈춤] 이 PC에 아직 저장(커밋)하지 않은 변경사항이 있습니다:
echo.
git status --short
echo.
echo   먼저 "커밋하고_배포.bat" 을 실행해서 저장한 뒤,
echo   이 파일을 다시 실행하세요.
echo   (버릴 작업이면 그냥 물어보세요 - 되돌리는 건 위험해서 자동으로 안 합니다)
goto end

:conflict
echo.
echo   [멈춤] 그냥 합쳐지지 않았습니다.
echo   위 메시지를 그대로 복사해서 물어보세요. 손대지 마세요.
goto end

:nogit
echo   [멈춤] git 이 설치되어 있지 않습니다.
echo          winget install -e --id Git.Git

:end
echo.
pause
