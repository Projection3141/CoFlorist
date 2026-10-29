@echo off
rem CoFlorist cat board USB bridge launcher (Windows). Double-click to run.
rem Extra options are passed through, e.g.  start-bridge.bat --port COM8
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python 3.8 or newer is required.
  echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  pause
  exit /b 1
)

if exist "vendor\serial" goto run
%PY% -c "import serial" >nul 2>nul && goto run
echo Installing pyserial ...
%PY% -m pip install --user -r requirements.txt
if errorlevel 1 (
  echo Could not install pyserial. Check the internet connection, or use the portable zip with vendor\serial.
  pause
  exit /b 1
)

:run
echo.
echo Dashboard: Settings - External conversation - http://127.0.0.1:4321/events
echo Press Ctrl+C to stop.
echo.
%PY% cat_usb_bridge.py %*
echo.
pause
