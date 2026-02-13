@echo off
setlocal EnableExtensions
title DustyBot
cd /d "%~dp0"

set "APP_SCRIPT=%~dp0Core_software\gui\gui_app.py"
set "REQ_FILE=%~dp0Core_software\requirements.txt"
set "VENV_DIR=%~dp0.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "DEPS_STAMP=%VENV_DIR%\.deps_hash"
set "BOOTSTRAP_ONLY="
if /I "%~1"=="--bootstrap-only" set "BOOTSTRAP_ONLY=1"

if not exist "%APP_SCRIPT%" (
    echo.
    echo  ERROR: App entrypoint not found:
    echo  %APP_SCRIPT%
    echo.
    pause
    exit /b 1
)

if not exist "%REQ_FILE%" (
    echo.
    echo  ERROR: requirements.txt not found:
    echo  %REQ_FILE%
    echo.
    pause
    exit /b 1
)

call :resolve_python
if errorlevel 1 (
    echo.
    echo  Python not found. Installing Python with winget...
    call :install_python
    if errorlevel 1 goto :fatal
    call :resolve_python
    if errorlevel 1 (
        echo.
        echo  ERROR: Python installation finished but Python still is not detected.
        echo  Close and reopen terminal, then run:
        echo    .\DustyBot.cmd
        goto :fatal
    )
)

echo.
echo  Python launcher: %PYTHON_DESC%
call :ensure_venv
if errorlevel 1 goto :fatal

call :install_python_deps
if errorlevel 1 goto :fatal

call :ensure_sumatra

if defined BOOTSTRAP_ONLY (
    echo.
    echo  Bootstrap complete.
    exit /b 0
)

echo.
echo  Starting DustyBot...
"%VENV_PY%" "%APP_SCRIPT%"
if errorlevel 1 (
    echo.
    echo  DustyBot exited with an error.
    pause
    exit /b 1
)
exit /b 0

:resolve_python
set "PYTHON_MODE="
set "PYTHON_PATH="
set "PYTHON_DESC="

where python >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_MODE=python"
    set "PYTHON_DESC=python"
    goto :resolve_python_done
)

where py >nul 2>&1
if not errorlevel 1 (
    py -3 -V >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_MODE=py"
        set "PYTHON_DESC=py -3"
        goto :resolve_python_done
    )
)

for /f "delims=" %%D in ('dir /b /ad "%LocalAppData%\Programs\Python\Python3*" 2^>nul ^| sort /r') do (
    if exist "%LocalAppData%\Programs\Python\%%D\python.exe" (
        set "PYTHON_MODE=path"
        set "PYTHON_PATH=%LocalAppData%\Programs\Python\%%D\python.exe"
        set "PYTHON_DESC=%LocalAppData%\Programs\Python\%%D\python.exe"
        goto :resolve_python_done
    )
)

:resolve_python_done
if not defined PYTHON_MODE exit /b 1
call :run_python -V >nul 2>&1
if errorlevel 1 exit /b 1
exit /b 0

:run_python
if /I "%PYTHON_MODE%"=="python" (
    python %*
    exit /b %errorlevel%
)
if /I "%PYTHON_MODE%"=="py" (
    py -3 %*
    exit /b %errorlevel%
)
if /I "%PYTHON_MODE%"=="path" (
    "%PYTHON_PATH%" %*
    exit /b %errorlevel%
)
exit /b 1

:install_python
where winget >nul 2>&1
if errorlevel 1 (
    echo.
    echo  ERROR: winget is not available on this PC.
    echo  Install Python manually from https://www.python.org/downloads/
    echo  Then rerun .\DustyBot.cmd
    exit /b 1
)

set "PYTHON_INSTALLED="
for %%I in (Python.Python.3.13 Python.Python.3.12 Python.Python.3) do (
    if defined PYTHON_INSTALLED goto :install_python_done
    echo.
    echo  Trying winget package %%I ...
    winget install --id %%I -e --accept-package-agreements --accept-source-agreements
    if not errorlevel 1 set "PYTHON_INSTALLED=1"
)

:install_python_done
if not defined PYTHON_INSTALLED (
    echo.
    echo  ERROR: Could not install Python automatically.
    exit /b 1
)
exit /b 0

:ensure_venv
if exist "%VENV_PY%" exit /b 0

echo.
echo  Creating local virtual environment...
call :run_python -m venv "%VENV_DIR%"
if errorlevel 1 (
    echo.
    echo  ERROR: Failed to create virtual environment.
    exit /b 1
)

if not exist "%VENV_PY%" (
    echo.
    echo  ERROR: Virtual environment created, but Python was not found at:
    echo  %VENV_PY%
    exit /b 1
)
exit /b 0

:install_python_deps
set "REQ_HASH="
for /f "tokens=1" %%H in ('certutil -hashfile "%REQ_FILE%" SHA256 ^| findstr /R /I "^[0-9A-F][0-9A-F]*$"') do (
    set "REQ_HASH=%%H"
    goto :got_req_hash
)

:got_req_hash
set "NEED_INSTALL=1"
set "OLD_HASH="
if defined REQ_HASH if exist "%DEPS_STAMP%" set /p OLD_HASH=<"%DEPS_STAMP%"
if defined REQ_HASH if exist "%DEPS_STAMP%" if /I "%OLD_HASH%"=="%REQ_HASH%" set "NEED_INSTALL="

if not defined NEED_INSTALL (
    echo.
    echo  Python dependencies are up to date.
    exit /b 0
)

echo.
echo  Installing Python dependencies...
"%VENV_PY%" -m ensurepip --upgrade >nul 2>&1
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 (
    echo.
    echo  ERROR: Failed to upgrade pip.
    exit /b 1
)

"%VENV_PY%" -m pip install -r "%REQ_FILE%"
if errorlevel 1 (
    echo.
    echo  ERROR: Failed to install dependencies from requirements.txt
    exit /b 1
)

if defined REQ_HASH > "%DEPS_STAMP%" echo %REQ_HASH%
echo.
echo  Dependencies installed.
exit /b 0

:find_sumatra
set "SUMATRA_EXE="

if exist "C:\Program Files\SumatraPDF\SumatraPDF.exe" set "SUMATRA_EXE=C:\Program Files\SumatraPDF\SumatraPDF.exe"
if not defined SUMATRA_EXE if exist "C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe" set "SUMATRA_EXE=C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe"
if not defined SUMATRA_EXE if exist "%LocalAppData%\SumatraPDF\SumatraPDF.exe" set "SUMATRA_EXE=%LocalAppData%\SumatraPDF\SumatraPDF.exe"
if not defined SUMATRA_EXE if exist "%AppData%\SumatraPDF\SumatraPDF.exe" set "SUMATRA_EXE=%AppData%\SumatraPDF\SumatraPDF.exe"
if defined SUMATRA_EXE exit /b 0

for /f "delims=" %%S in ('where SumatraPDF.exe 2^>nul') do (
    set "SUMATRA_EXE=%%~fS"
    exit /b 0
)

for /f "delims=" %%S in ('where SumatraPDF 2^>nul') do (
    set "SUMATRA_EXE=%%~fS"
    exit /b 0
)

exit /b 1

:ensure_sumatra
call :find_sumatra
if not errorlevel 1 (
    echo.
    echo  SumatraPDF: %SUMATRA_EXE%
    exit /b 0
)

echo.
echo  SumatraPDF not found. Installing with winget...
where winget >nul 2>&1
if errorlevel 1 (
    echo  WARNING: winget is missing, so SumatraPDF was not installed.
    echo  Printing will not work until SumatraPDF is installed.
    exit /b 0
)

winget install --id SumatraPDF.SumatraPDF -e --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
    echo  WARNING: SumatraPDF install failed. Printing may not work.
    exit /b 0
)

call :find_sumatra
if errorlevel 1 (
    echo  WARNING: SumatraPDF installed but not detected yet.
    echo  Printing may work after reopening terminal.
) else (
    echo  SumatraPDF: %SUMATRA_EXE%
)
exit /b 0

:fatal
echo.
echo  Setup failed.
pause
exit /b 1
