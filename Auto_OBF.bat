@echo off
setlocal

cd /d "%~dp0"

REM =====================================================================
REM  Auto_OBF.bat - build the protected release from the current source.
REM
REM  Everything lands in a folder called "RELEASE BUILDS". Only compiled
REM  artifacts go there: rcfb.exe, rcfb-cli.exe, .env, start.bat, HOWTO.txt
REM  and BUILD_REPORT.txt. No Python source is ever copied into it.
REM
REM  How the source is protected:
REM    * Nuitka compiles both programs to C and then to native code, so the
REM      shipped binaries contain no readable Python.
REM    * --python-flag=no_docstrings strips every docstring, removing the
REM      comments that describe how the internals work.
REM    * The release is given its own password instead of the project's
REM      development one. It is written to the release .env AND bundled into
REM      rcfb.exe, so the exe and the file beside it always agree.
REM    * The folder is audited at the end; the build fails if any .py,
REM      __pycache__ or other source leftover reached it.
REM
REM  Delete "RELEASE BUILDS\.env" to force a new password on the next run.
REM =====================================================================

set "OUT=RELEASE BUILDS"
set "RELENV=%OUT%\.env"

REM --- prerequisites ---------------------------------------------------
where python >nul 2>nul || (echo [ERROR] python not found on PATH & pause & exit /b 1)
if not exist "server.py" (echo [ERROR] server.py not found in %CD% & pause & exit /b 1)
if not exist "rcfb_cli.py" (echo [ERROR] rcfb_cli.py not found in %CD% & pause & exit /b 1)
if not exist "static\index.html" (echo [ERROR] static\index.html not found in %CD% & pause & exit /b 1)

python -c "import nuitka" 2>nul
if errorlevel 1 (
    echo [*] Installing Nuitka...
    python -m pip install nuitka || (echo [ERROR] pip install nuitka failed & pause & exit /b 1)
)

if not exist "%OUT%" mkdir "%OUT%"

REM A stale exe from an earlier run must not be mistaken for this build.
if exist "%OUT%\rcfb.exe" del /q "%OUT%\rcfb.exe"
if exist "%OUT%\rcfb-cli.exe" del /q "%OUT%\rcfb-cli.exe"

REM --- the release password --------------------------------------------
REM Generated once and then reused, so rebuilding does not silently change
REM the password of a release already in use.
if not exist "%RELENV%" (
    echo [*] Generating a release password...
    python -c "import secrets,string;al=string.ascii_letters+string.digits+'-_.*+';pw=''.join(secrets.choice(al) for _ in range(32));open(r'%RELENV%','w',encoding='utf-8').write('# Password for the rcfb login (username: Root)\nPASSWORD='+pw+'\n')" || (echo [ERROR] could not write %RELENV% & pause & exit /b 1)
) else (
    echo [*] Reusing the existing %RELENV%
)

REM --- the server ------------------------------------------------------
echo.
echo [*] Building %OUT%\rcfb.exe ...
python -m nuitka ^
    --onefile ^
    --assume-yes-for-downloads ^
    --windows-console-mode=force ^
    --python-flag=no_docstrings ^
    --output-dir="%OUT%" ^
    --output-filename=rcfb.exe ^
    --remove-output ^
    --include-data-dir=static=static ^
    --include-data-files="%RELENV%=.env" ^
    --include-module=pynput.keyboard._win32 ^
    --include-module=pynput.mouse._win32 ^
    server.py
if errorlevel 1 (echo [ERROR] rcfb.exe build failed & pause & exit /b 1)

REM --- the control panel ------------------------------------------------
echo.
echo [*] Building %OUT%\rcfb-cli.exe ...
python -m nuitka ^
    --onefile ^
    --assume-yes-for-downloads ^
    --windows-console-mode=force ^
    --python-flag=no_docstrings ^
    --output-dir="%OUT%" ^
    --output-filename=rcfb-cli.exe ^
    --remove-output ^
    rcfb_cli.py
if errorlevel 1 (echo [ERROR] rcfb-cli.exe build failed & pause & exit /b 1)

REM --- supporting files -------------------------------------------------
REM start.bat and HOWTO.txt ship from OBF\, which holds the compiled-server
REM versions. The copy in the project root launches server.py, which is not
REM what a release runs.
if exist "OBF\start.bat" (copy /y "OBF\start.bat" "%OUT%\start.bat" >nul) else (echo [WARN] OBF\start.bat not found - the release has no launcher)
if exist "OBF\HOWTO.txt" (copy /y "OBF\HOWTO.txt" "%OUT%\HOWTO.txt" >nul) else (echo [WARN] OBF\HOWTO.txt not found - the release has no instructions)

REM --- audit: nothing readable may have been shipped --------------------
set "LEAK=0"
for %%F in ("%OUT%\*.py") do (echo [ERROR] source file reached the release: %%F & set "LEAK=1")
for %%F in ("%OUT%\*.pyc") do (echo [ERROR] compiled source reached the release: %%F & set "LEAK=1")
for /d %%D in ("%OUT%\*") do (echo [ERROR] unexpected folder in the release: %%D & set "LEAK=1")
if exist "%OUT%\__pycache__" (echo [ERROR] __pycache__ reached the release & set "LEAK=1")
if "%LEAK%"=="1" (echo [ERROR] release contains source - aborting & pause & exit /b 1)

REM --- report -----------------------------------------------------------
python -c "import hashlib,os,sys,datetime;p=os.path.join(r'%OUT%','BUILD_REPORT.txt');f=open(p,'w',encoding='utf-8');f.write('BUILD REPORT\n============\n\nBuilt  : '+datetime.datetime.now().strftime('%%Y-%%m-%%d %%H:%%M:%%S')+'\nTool   : Nuitka, onefile, docstrings stripped\n\n');[f.write('%%-14s %%s\n               %%s\n'%%(n,str(os.path.getsize(os.path.join(r'%OUT%',n)))+' bytes',hashlib.sha256(open(os.path.join(r'%OUT%',n),'rb').read()).hexdigest())) for n in ('rcfb.exe','rcfb-cli.exe')];f.write('\nPassword: see .env in this folder. The same password is bundled\n          inside rcfb.exe as its fallback, so the two agree.\n');f.close();print('  wrote',p)" || echo [WARN] could not write the build report

REM --- done -------------------------------------------------------------
echo.
echo ============================================================
echo  Release built in "%OUT%"
echo.
dir /b "%OUT%"
echo.
echo  The login password is in "%RELENV%":
type "%RELENV%"
echo.
echo  The CLI checks the project version against its public control
echo  repository on start. If that version is not published there the
echo  CLI stops with "Bad version detected." - publish it, or run the
echo  CLI from a folder with no project source beside it.
echo ============================================================
echo.

endlocal
