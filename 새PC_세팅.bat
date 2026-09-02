@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title The Associate - 새 PC 세팅

echo ==================================================
echo    The Associate - 새 PC 세팅
echo    이 창이 다 알아서 합니다. 그냥 지켜보세요.
echo ==================================================
echo.

REM ---------- 1. Python 찾기 ----------
echo [1/6] Python 찾는 중...
set "PYCMD="
py -3.12 --version >nul 2>&1
if %errorlevel% equ 0 goto py312
py -3 --version >nul 2>&1
if %errorlevel% equ 0 goto py3
python --version >nul 2>&1
if %errorlevel% equ 0 goto pyplain

echo.
echo   [멈춤] Python 이 없습니다.
echo.
echo   아래 명령을 이 창에 붙여넣고 Enter 를 누르세요 (설치 후 이 파일을 다시 실행):
echo.
echo       winget install -e --id Python.Python.3.12
echo.
echo   winget 이 안 되면 https://www.python.org/downloads/ 에서 3.12 를 받아
echo   설치할 때 "Add python.exe to PATH" 를 꼭 체크하세요.
echo.
pause
exit /b 1

:py312
set "PYCMD=py -3.12"
goto pyok
:py3
set "PYCMD=py -3"
goto pyok
:pyplain
set "PYCMD=python"
:pyok
for /f "delims=" %%v in ('%PYCMD% --version 2^>^&1') do echo   찾음: %%v
echo.

REM ---------- 2. 가상환경 ----------
echo [2/6] 가상환경(.venv) 준비...
if exist ".venv\Scripts\python.exe" (
  echo   이미 있음 - 건너뜀
) else (
  %PYCMD% -m venv .venv
  if errorlevel 1 (
    echo   [실패] 가상환경을 만들지 못했습니다.
    pause
    exit /b 1
  )
  echo   생성 완료
)
echo.

REM ---------- 3. 패키지 ----------
echo [3/6] 패키지 설치 중... (처음이면 2~5분 걸립니다)
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo   [실패] 패키지 설치 중 오류. 위 메시지를 그대로 복사해서 물어보세요.
  pause
  exit /b 1
)
echo   설치 완료
echo.

REM ---------- 4. .env ----------
echo [4/6] 키 파일(.env) 준비...
if exist ".env" (
  echo   이미 있음 - 비어있는 키만 아래에서 물어봅니다
) else (
  copy ".env.example" ".env" >nul
  echo   .env.example 을 복사해 .env 를 만들었습니다
)
echo.

REM ---------- 5. 키 입력 ----------
echo [5/6] API 키 입력
echo   - 이미 채워진 키는 자동으로 건너뜁니다
echo   - 모르는 키는 그냥 Enter 를 누르고 넘어가세요 (나중에 다시 넣을 수 있음)
echo   - 입력한 값은 화면에 보이지 않습니다
echo.
".venv\Scripts\python.exe" _set_keys.py
echo.

REM ---------- 6. git 신원 ----------
echo [6/6] git 확인...
git --version >nul 2>&1
if errorlevel 1 (
  echo   [주의] git 이 없습니다. 코드 저장/배포를 하려면 필요합니다.
  echo          winget install -e --id Git.Git
  goto done
)
for /f "delims=" %%n in ('git config user.name 2^>nul') do set "GITNAME=%%n"
if defined GITNAME goto gitok
echo   git 에 이름/이메일이 설정되어 있지 않습니다. 지금 넣어주세요.
set /p GN="  이름 (예: sanghwa): "
set /p GE="  이메일 (예: sanghwalee@sksquare.com): "
git config --global user.name "%GN%"
git config --global user.email "%GE%"
echo   설정 완료
goto done
:gitok
echo   git 신원 확인됨: %GITNAME%

:done
echo.
echo ==================================================
echo    세팅 끝!
echo.
echo    이제 이렇게 쓰세요 (전부 더블클릭):
echo      SKSQ_실행.bat        - 앱 켜기 (localhost:8501)
echo      최신코드_받기.bat     - 다른 PC에서 한 작업 가져오기
echo      커밋하고_배포.bat     - 내 작업 저장 + Railway 배포
echo ==================================================
echo.
pause
