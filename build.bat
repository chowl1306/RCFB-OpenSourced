@echo off
setlocal

cd /d "%~dp0"

REM Builds rcfb.exe: a single self contained file (Nuitka onefile).
REM
REM   static/  and .env are unpacked into the temporary directory Nuitka uses
REM            at runtime, which is where __file__ points, so Flask finds its
REM            static folder and load_env() finds the bundled password.
REM   transfers/ is created next to the executable (see BASE_DIR in server.py)
REM            because the onefile temporary directory is deleted on exit.
REM
REM pynput picks its backend with importlib at runtime, which static analysis
REM cannot see, so the Windows backends are named explicitly.

where python >nul 2>nul || (echo [ERROR] python not found on PATH & pause & exit /b 1)
if not exist "server.py" (echo [ERROR] server.py not found in %CD% & pause & exit /b 1)
if not exist ".env" (echo [ERROR] .env not found in %CD% & pause & exit /b 1)
if not exist "static\index.html" (echo [ERROR] static\index.html not found in %CD% & pause & exit /b 1)

python -c "import nuitka" 2>nul
if errorlevel 1 (
    echo [*] Installing Nuitka...
    python -m pip install nuitka || (echo [ERROR] pip install nuitka failed & pause & exit /b 1)
)

echo [*] Building build\rcfb.exe ...
python -m nuitka ^
    --onefile ^
    --assume-yes-for-downloads ^
    --windows-console-mode=force ^
    --output-dir=build ^
    --output-filename=rcfb.exe ^
    --remove-output ^
    --include-data-dir=static=static ^
    --include-data-files=.env=.env ^
    --include-module=pynput.keyboard._win32 ^
    --include-module=pynput.mouse._win32 ^
    server.py
if errorlevel 1 (echo [ERROR] Nuitka build failed & pause & exit /b 1)

echo.
echo ============================================================
echo  Built build\rcfb.exe
echo  Run it from any directory; transfers\ is created next to it.
echo ============================================================
echo.

endlocal
