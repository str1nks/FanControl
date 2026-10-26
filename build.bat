@echo off
rem ===== Build portable FanControl.exe (everything inside a venv) =====
rem Put fans_gui.py, icon.ico and this file in one folder, then run build.bat.
rem Needs 64-bit Python 3.11 or 3.12 (python.org) with "Add to PATH" ticked.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python not found in PATH.
  goto fail
)

if not exist "venv\Scripts\python.exe" (
  echo Creating virtual environment venv ...
  python -m venv venv
  if errorlevel 1 goto fail
)
set "PY=%~dp0venv\Scripts\python.exe"

"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install PySide6 HardwareMonitor pyinstaller
if errorlevel 1 goto fail

"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin ^
  --name FanControl --icon icon.ico --add-data "icon.ico;." ^
  --collect-all HardwareMonitor --collect-all pythonnet --collect-all clr_loader ^
  --hidden-import clr ^
  fans_gui.py
if errorlevel 1 goto fail

echo.
echo DONE: dist\FanControl.exe  (copy it anywhere, no install needed)
echo The venv, build and dist\*.spec files can be deleted if you want.
pause
exit /b 0

:fail
echo.
echo BUILD FAILED - see the messages above.
pause
exit /b 1