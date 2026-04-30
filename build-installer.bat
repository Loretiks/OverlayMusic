@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem --- 1. Make sure exe is built --------------------------------------
if not exist "dist\SpotifyOverlay.exe" (
    echo [1/2] Building exe first...
    call build.bat
    if errorlevel 1 exit /b 1
) else (
    echo [1/2] dist\SpotifyOverlay.exe already exists — skipping rebuild.
    echo       Delete the dist\ folder if you want a fresh exe.
)

rem --- 2. Locate Inno Setup (6 or 7) -----------------------------------
set "ISCC="
for %%V in (7 6) do (
    if "!ISCC!"=="" if exist "%ProgramFiles%\Inno Setup %%V\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup %%V\ISCC.exe"
    if "!ISCC!"=="" if exist "%ProgramFiles(x86)%\Inno Setup %%V\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup %%V\ISCC.exe"
    if "!ISCC!"=="" if exist "%LocalAppData%\Programs\Inno Setup %%V\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup %%V\ISCC.exe"
)

if "%ISCC%"=="" (
    echo.
    echo Inno Setup 6 not found.
    echo.
    echo Install it ^(one-time^):
    echo   winget install --id JRSoftware.InnoSetup -e
    echo Or download manually: https://jrsoftware.org/isdl.php
    echo.
    pause
    exit /b 1
)

echo [2/2] Compiling installer with: "%ISCC%"
if not exist installer mkdir installer

"%ISCC%" installer.iss
if errorlevel 1 (
    echo.
    echo Installer compilation failed.
    pause
    exit /b 1
)

echo.
echo ====================================================
echo  Done! See installer\SpotifyOverlay-Setup-*.exe
echo ====================================================
echo.
pause
exit /b 0
