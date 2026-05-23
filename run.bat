@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

rem === 1) Check for Python ===
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python is not on PATH.
    echo Install it from https://www.python.org/downloads/
    pause
    exit /b 1
)

rem === 2) Create the venv if missing ===
if not exist ".venv\Scripts\python.exe" (
    echo [SETUP] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create the venv.
        pause
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [ERROR] Failed to activate the venv.
    pause
    exit /b 1
)

rem === 3) Install deps if missing ===
python -c "import dotenv, requests, rich" >nul 2>nul
if errorlevel 1 (
    echo [SETUP] Installing dependencies...
    python -m pip install --upgrade pip --quiet
    python -m pip install -r requirements.txt --quiet
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
)

rem === 4) .env presence check ===
if not exist ".env" (
    if exist ".env.example" (
        echo [SETUP] Copying .env.example -^> .env
        copy /Y ".env.example" ".env" >nul
        echo.
        echo Fill in CAPSOLVER_API_KEY and EMAIL_DOMAINS in .env.
        echo The app will ask for your license key on first run.
        echo Need a license? Buy one at https://t.me/putrm
        notepad ".env"
        pause
        exit /b 0
    ) else (
        echo [ERROR] Neither .env nor .env.example exist.
        pause
        exit /b 1
    )
)

rem === 5) Pass-through mode (args provided directly) ===
if not "%~1"=="" (
    python main.py %*
    set EXITCODE=!ERRORLEVEL!
    echo.
    pause
    exit /b !EXITCODE!
)

rem === 6) Interactive prompt (Python handles it for consistent colors) ===
python -m _prompt
set EXITCODE=!ERRORLEVEL!
if !EXITCODE! NEQ 0 (
    pause
    exit /b !EXITCODE!
)

rem _prompt.py writes the chosen args to .last_args; read and forward them.
if exist .last_args (
    set /p ARGS=<.last_args
    del .last_args >nul 2>&1
    python main.py !ARGS!
    set EXITCODE=!ERRORLEVEL!
)

echo.
pause
exit /b !EXITCODE!
