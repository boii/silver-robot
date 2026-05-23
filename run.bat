@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

rem === 1) Cek Python ===
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python tidak ditemukan di PATH.
    echo Install dari https://www.python.org/downloads/
    pause
    exit /b 1
)

rem === 2) Buat venv ===
if not exist ".venv\Scripts\python.exe" (
    echo [SETUP] Membuat virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Gagal membuat venv.
        pause
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [ERROR] Gagal aktivasi venv.
    pause
    exit /b 1
)

rem === 3) Install deps kalau belum ===
python -c "import dotenv, requests, rich" >nul 2>nul
if errorlevel 1 (
    echo [SETUP] Menginstal dependencies...
    python -m pip install --upgrade pip --quiet
    python -m pip install -r requirements.txt --quiet
    if errorlevel 1 (
        echo [ERROR] Gagal install dependencies.
        pause
        exit /b 1
    )
)

rem === 4) .env check ===
if not exist ".env" (
    if exist ".env.example" (
        echo [SETUP] Menyalin .env.example -^> .env
        copy /Y ".env.example" ".env" >nul
        echo.
        echo Isi dulu CAPSOLVER_API_KEY dan EMAIL_DOMAINS di .env
        notepad ".env"
        pause
        exit /b 0
    ) else (
        echo [ERROR] File .env dan .env.example tidak ada.
        pause
        exit /b 1
    )
)

rem === 5) Mode argumen langsung ===
if not "%~1"=="" (
    python main.py %*
    set EXITCODE=!ERRORLEVEL!
    echo.
    pause
    exit /b !EXITCODE!
)

rem === 6) Prompt interaktif (delegasi ke Python supaya warna konsisten) ===
python -m _prompt
set EXITCODE=!ERRORLEVEL!
if !EXITCODE! NEQ 0 (
    pause
    exit /b !EXITCODE!
)

rem File _prompt.py menulis pilihan ke .last_args, kita baca di sini
if exist .last_args (
    set /p ARGS=<.last_args
    del .last_args >nul 2>&1
    python main.py !ARGS!
    set EXITCODE=!ERRORLEVEL!
)

echo.
pause
exit /b !EXITCODE!
