@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title The Associate - 저장하고 배포

echo ==================================================
echo    내 작업 저장 + Railway 배포
echo ==================================================
echo.

git --version >nul 2>&1
if errorlevel 1 goto nogit

for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD') do set "BR=%%b"
if not "%BR%"=="main" echo   [주의] 지금 브랜치가 main 이 아니라 "%BR%" 입니다. 배포는 main 에서만 나갑니다.

git status --porcelain > "%TEMP%\_ta_status.txt"
for %%s in ("%TEMP%\_ta_status.txt") do if %%~zs equ 0 goto nothing

echo 이번에 올라갈 변경사항:
echo --------------------------------------------------
git status --short
echo --------------------------------------------------
echo.

set "MSG="
set /p MSG="무엇을 바꿨는지 한 줄로 적으세요: "
if not defined MSG goto nomsg

echo.
echo   push 하면 GitHub 테스트를 거쳐 Railway 에 자동 배포됩니다 (실서비스가 바뀝니다).
set "OK="
set /p OK="정말 올릴까요? (y/n): "
if /i not "%OK%"=="y" goto cancelled

echo.
git add -A
git commit -m "%MSG%"
if errorlevel 1 goto failed

echo.
echo 원격 최신본과 맞추는 중...
git pull --rebase origin main
if errorlevel 1 goto rebasefail

git push origin main
if errorlevel 1 goto failed

echo.
echo ==================================================
echo    올렸습니다!
echo    CI 진행:  https://github.com/aen6602-pixel/the-associate/actions
echo    테스트가 초록불이 되면 Railway 가 자동으로 배포합니다 (보통 3~5분).
echo ==================================================
goto end

:nothing
echo   바뀐 게 없습니다. 올릴 것이 없어요.
goto end

:nomsg
echo   설명을 안 적어서 취소했습니다.
goto end

:cancelled
echo   취소했습니다. 아무것도 올라가지 않았습니다.
goto end

:rebasefail
echo.
echo   [멈춤] 다른 PC에서 올린 작업과 겹칩니다.
echo   위 메시지를 그대로 복사해서 물어보세요. 손대지 마세요.
echo   (되돌리려면: git rebase --abort)
goto end

:failed
echo.
echo   [멈춤] 실패했습니다. 위 메시지를 그대로 복사해서 물어보세요.
goto end

:nogit
echo   [멈춤] git 이 설치되어 있지 않습니다.
echo          winget install -e --id Git.Git

:end
echo.
pause
