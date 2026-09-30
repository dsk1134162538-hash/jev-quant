@echo off
rem Home one-click setup for jev-quant (paper trading daemon + live follower).
rem Double-click this file in the dev directory (E:\claude\jev-quant). Safe to run again.
rem It re-runs itself from %TEMP% so that "git pull" can update this very file safely.
rem "home_setup.bat --force" also installs/restarts during trading hours (09:15-15:30 on weekdays).
rem Use --force only when nothing is running yet (first install); never while live.py is trading.
if /i not "%~1"=="--run" (
  set "JEVQ_FORCE="
  if /i "%~1"=="--force" set "JEVQ_FORCE=--force"
  copy /y "%~f0" "%TEMP%\jevq_home_setup.bat" >nul
  call "%TEMP%\jevq_home_setup.bat" --run "%~dp0"
  exit /b
)
setlocal
cd /d "%~2"
chcp 65001 >nul

echo.
echo ==== [1/5] Update code: git pull master ====
git fetch origin || goto fail
git checkout master || goto fail
git pull --ff-only origin master || goto fail

echo.
echo ==== [2/5] Python environment .venv ====
if not exist ".venv\Scripts\python.exe" (
  py -3.12 -m venv .venv || python -m venv .venv || goto fail
)
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto fail

echo.
echo ==== [3/5] easytrader (backup broker, optional) ====
".venv\Scripts\python.exe" -m pip install -q "git+https://github.com/shidenggui/easytrader@def5a8edc5ebd7d49bf92a9f39e9e30c9bf47101" pywin32 || echo easytrader install failed - it is only the backup broker, continuing

echo.
echo ==== [4/5] xiadan-gateway + Tailscale address ====
".venv\Scripts\python.exe" scripts\live_install.py %JEVQ_FORCE% || goto fail

echo.
echo ==== [5/5] Run directory E:\jev-live + daemon.py + live.py ====
".venv\Scripts\python.exe" scripts\home_install.py %JEVQ_FORCE% || goto fail

echo.
echo ==== ALL DONE ====
pause
exit /b 0

:fail
echo.
echo ==== FAILED - take a screenshot of this window and send it to Claude ====
pause
exit /b 1
