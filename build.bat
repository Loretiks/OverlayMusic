@echo off
setlocal
cd /d "%~dp0"

echo [1/3] Setting up venv...
if not exist .venv (
    py -m venv .venv
    if errorlevel 1 goto :error
)
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install pyinstaller
if errorlevel 1 goto :error

echo.
echo [2/3] Cleaning previous build...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist OverlayMusic.spec del /q OverlayMusic.spec

echo.
echo [3/3] Building exe (this may take a couple of minutes)...
.venv\Scripts\pyinstaller.exe ^
    --noconfirm ^
    --onefile ^
    --windowed ^
    --name "OverlayMusic" ^
    --collect-all winrt ^
    overlay.py
if errorlevel 1 goto :error

echo.
echo ====================================================
echo  Done! File: dist\OverlayMusic.exe
echo  You can copy it anywhere and run from there.
echo ====================================================
echo.
pause
exit /b 0

:error
echo.
echo Build failed. See messages above.
pause
exit /b 1
