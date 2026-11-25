@echo off
setlocal

cd /d "%~dp0"

REM Builds rcfb-cli.exe: a single self contained file (Nuitka onefile).
REM
REM The CLI is pure standard library, so unlike build.bat there is nothing to
REM unpack at runtime: no static folder, no bundled .env, and no importlib
REM backend to name explicitly. The only file it needs at build time is
REM rcfb_cli.py itself.
REM
REM The build already accounts for being frozen. rcfb_cli.py reads Nuitka's
REM __compiled__ / containing_dir, so in the onefile build the folder holding
REM the exe is what search_dirs() looks in for rcfb.exe or server.py, and
REM python_executable() knows not to trust sys.executable as an interpreter.
REM
REM Output goes to build\ next to rcfb.exe from build.bat, which is the layout
REM a release folder uses: rcfb-cli.exe finds rcfb.exe beside it.

where python >nul 2>nul || (echo [ERROR] python not found on PATH & pause & exit /b 1)
if not exist "rcfb_cli.py" (echo [ERROR] rcfb_cli.py not found in %CD% & pause & exit /b 1)

python -c "import nuitka" 2>nul
if errorlevel 1 (
    echo [*] Installing Nuitka...
    python -m pip install nuitka || (echo [ERROR] pip install nuitka failed & pause & exit /b 1)
)

echo [*] Building build\rcfb-cli.exe ...
python -m nuitka ^
    --onefile ^
    --assume-yes-for-downloads ^
    --windows-console-mode=force ^
    --output-dir=build ^
    --output-filename=rcfb-cli.exe ^
    --remove-output ^
    rcfb_cli.py
if errorlevel 1 (echo [ERROR] Nuitka build failed & pause & exit /b 1)

echo.
echo ============================================================
echo  Built build\rcfb-cli.exe
echo  Put it beside rcfb.exe; it runs that server and reads the
echo  version from the project's CHANGELOGS.txt.
echo ============================================================
echo.

endlocal
