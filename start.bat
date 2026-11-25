@echo off
setlocal

cd /d "%~dp0"

REM --- sanity checks -------------------------------------------------------
where python >nul 2>nul || (echo [ERROR] python not found on PATH & pause & exit /b 1)
where cloudflared >nul 2>nul || (echo [ERROR] cloudflared not found on PATH & pause & exit /b 1)
if not exist "server.py" (echo [ERROR] server.py not found in %CD% & pause & exit /b 1)
if not exist ".env" (
    echo [WARN] .env not found. The login page needs PASSWORD=... in .env
    pause
)

REM --- dependencies (only if needed) ---------------------------------------
python -c "import flask, pynput, mss, PIL, pyperclip" 2>nul
if errorlevel 1 (
    echo [*] Installing Python dependencies...
    python -m pip install -r requirements.txt || (echo [ERROR] pip install failed & pause & exit /b 1)
)

REM --- start Flask server in its own window --------------------------------
echo [*] Starting rcfb server on http://127.0.0.1:6767 ...
start "rcfb server" cmd /k "cd /d ""%CD%"" && python server.py"

REM --- wait for the server to accept connections ---------------------------
echo [*] Waiting for server to come up...
:wait
timeout /t 1 /nobreak >nul
powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:6767/login -TimeoutSec 2) | Out-Null; exit 0 } catch { exit 1 }" >nul 2>nul
if errorlevel 1 goto wait

REM --- start cloudflared quick tunnel in its own window --------------------
echo [*] Starting cloudflared quick tunnel...
start "rcfb tunnel" cmd /k "cloudflared tunnel --url localhost:6767 --no-autoupdate"

echo.
echo ============================================================
echo  rcfb is running.
echo    Local:  http://127.0.0.1:6767
echo    Public: look for the https://*.trycloudflare.com line in
echo            the "rcfb tunnel" window that just opened.
echo  Close either window (or press Ctrl+C in it) to stop that part.
echo ============================================================
echo.

endlocal