@echo off
setlocal
cd /d "%~dp0"
title Weather warning outlook (local)

where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py -3"
  goto run
)
python --version >nul 2>nul
if %errorlevel%==0 (
  set "PY=python"
  goto run
)

echo.
echo  Python is not installed on this computer.
echo  1. A download page will open. Install Python (keep the default options).
echo  2. Then double-click start.bat again.
echo.
start "" "https://www.python.org/downloads/"
pause
exit /b 1

:run
%PY% -m teukbo.local %*
echo.
pause
