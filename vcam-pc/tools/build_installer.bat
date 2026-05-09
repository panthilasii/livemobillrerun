@echo off
REM NP Create -- one-command Windows installer build.
REM
REM Run this on a Windows host with:
REM   - Python 3.13 (with `pip install pyinstaller`)
REM   - Inno Setup 6 (https://jrsoftware.org/isinfo.php)
REM
REM Output:
REM   vcam-pc\dist\installer\NP-Create-Setup-<version>.exe
REM
REM This batch file is ASCII-only by design (cmd.exe parses .bat
REM files using the OEM codepage, so non-ASCII bytes break parsing
REM on customer machines -- same lesson as run.bat).

setlocal enableextensions enabledelayedexpansion

cd /d "%~dp0\.."
echo.
echo  ============================================================
echo   NP Create -- Windows Installer Build
echo  ============================================================
echo.

REM ---- 1. Resolve Python ---------------------------------------
set "PYBIN="
where py >nul 2>&1 && set "PYBIN=py -3"
if "%PYBIN%"=="" (
    where python >nul 2>&1 && set "PYBIN=python"
)
if "%PYBIN%"=="" (
    echo  [!] Python 3 is not installed. Install from python.org first.
    exit /b 1
)
echo  [1/4] Using Python: %PYBIN%

REM ---- 2. PyInstaller bundle (NP-Create.exe) -------------------
echo.
echo  [2/4] Building PyInstaller bundle...
%PYBIN% tools\build_pyinstaller.py
if errorlevel 1 (
    echo  [!] PyInstaller step failed. See output above.
    exit /b 1
)
if not exist dist\pyinstaller\NP-Create.exe (
    echo  [!] dist\pyinstaller\NP-Create.exe was not produced.
    exit /b 1
)
echo  [2/4] OK -- dist\pyinstaller\NP-Create.exe

REM ---- 3. Locate Inno Setup compiler ---------------------------
set "ISCC="
for %%P in (
    "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles%\Inno Setup 6\ISCC.exe"
) do (
    if exist %%~P set "ISCC=%%~P"
)
if "%ISCC%"=="" (
    echo  [!] Inno Setup 6 not found.
    echo      Download: https://jrsoftware.org/isinfo.php
    exit /b 1
)
echo  [3/4] Using Inno Setup: "%ISCC%"

REM ---- 4. Read canonical version from branding.py --------------
REM We pass it via /DMyAppVersion=... so installer.iss doesn't need
REM hand-editing every release. branding.py is the single source of
REM truth; if this command fails, fall back to whatever the .iss
REM has hardcoded (which CI will eventually flag).
for /f "usebackq delims=" %%V in (`%PYBIN% -c "import sys; sys.path.insert(0, 'src'); from branding import BRAND; print(BRAND.version)"`) do set "VER=%%V"
if "%VER%"=="" (
    echo  [!] Could not read version from src/branding.py -- using .iss default.
    set "VER_ARG="
) else (
    echo  [4/4] Building installer for version %VER%...
    set "VER_ARG=/DMyAppVersion=%VER%"
)

REM ---- 5. Compile installer ------------------------------------
"%ISCC%" %VER_ARG% tools\installer.iss
if errorlevel 1 (
    echo  [!] Inno Setup compile failed.
    exit /b 1
)

echo.
echo  ============================================================
echo   DONE.
echo  ============================================================
for %%F in (dist\installer\NP-Create-Setup-*.exe) do (
    echo   Output: %%F
    for %%S in ("%%F") do echo   Size:   %%~zS bytes
)
echo.
endlocal
exit /b 0
