@echo off
rem ============================================================================
rem  BBVA GPT Code Grabber - Public Release
rem  Versi ini menambah license gate di depan flow run.bat.
rem  Tidak memodifikasi run.bat / run-debug.bat (build internal tetap utuh).
rem
rem  License server: https://github.com/boii/license (running di VPS)
rem  Activation:   run-public.bat (akan minta key kalau belum tersimpan)
rem  Deactivate:   run-public.bat --license-deactivate
rem  Status:       run-public.bat --license-status
rem  Reset state:  run-public.bat --license-clear
rem ============================================================================
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

rem === 5) Sub-perintah lisensi (opsional) ===
if /I "%~1"=="--license-status" (
    python -m _license status
    pause
    exit /b 0
)
if /I "%~1"=="--license-deactivate" (
    python -m _license deactivate
    set EXITCODE=!ERRORLEVEL!
    pause
    exit /b !EXITCODE!
)
if /I "%~1"=="--license-clear" (
    python -m _license clear
    pause
    exit /b 0
)

rem === 6) License gate ===
rem  Coba check dulu (kalau key sudah pernah di-activate, ini cukup).
python -m _license check >nul 2>nul
set LIC_RC=!ERRORLEVEL!

if !LIC_RC! EQU 0 goto :license_ok

if !LIC_RC! EQU 4 (
    rem Belum ada key tersimpan -> minta input lalu activate.
    echo.
    echo === Aktivasi Lisensi ===
    echo Masukkan license key yang Anda terima saat pembelian.
    echo Format: XXXXX-XXXXX-XXXXX-XXXXX
    echo.
    set /p LICKEY=License Key: 
    if "!LICKEY!"=="" (
        echo [ERROR] Key kosong. Dibatalkan.
        pause
        exit /b 1
    )
    python -m _license activate "!LICKEY!"
    set ACT_RC=!ERRORLEVEL!
    if !ACT_RC! NEQ 0 (
        echo.
        echo [ERROR] Aktivasi gagal. Cek key Anda atau hubungi penjual.
        pause
        exit /b !ACT_RC!
    )
    goto :license_ok
)

if !LIC_RC! EQU 3 (
    echo.
    echo [ERROR] Tidak bisa terhubung ke server lisensi dan grace period offline habis.
    echo         Periksa koneksi internet Anda dan coba lagi.
    pause
    exit /b 3
)

if !LIC_RC! EQU 2 (
    echo.
    echo [ERROR] Lisensi tidak valid (revoked / expired / limit reached).
    echo         Hubungi penjual untuk perpanjangan atau reset slot.
    pause
    exit /b 2
)

echo.
echo [ERROR] License check gagal dengan exit code !LIC_RC!.
pause
exit /b !LIC_RC!

:license_ok

rem === 7) Mode argumen langsung ===
if not "%~1"=="" (
    python main.py %*
    set EXITCODE=!ERRORLEVEL!
    echo.
    pause
    exit /b !EXITCODE!
)

rem === 8) Prompt interaktif ===
python -m _prompt
set EXITCODE=!ERRORLEVEL!
if !EXITCODE! NEQ 0 (
    pause
    exit /b !EXITCODE!
)

if exist .last_args (
    set /p ARGS=<.last_args
    del .last_args >nul 2>&1
    python main.py !ARGS!
    set EXITCODE=!ERRORLEVEL!
)

echo.
pause
exit /b !EXITCODE!
