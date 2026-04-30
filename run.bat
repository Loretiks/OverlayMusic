@echo off
setlocal
cd /d "%~dp0"

if not exist .venv (
    echo [setup] Creating virtual env...
    py -m venv .venv
    if errorlevel 1 goto :error
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 goto :error
)

start "" .venv\Scripts\pythonw.exe overlay.py
exit /b 0

:error
echo.
echo Setup failed. Press any key to close.
pause >nul
exit /b 1
