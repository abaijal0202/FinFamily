@echo off
REM ============================================================
REM  FinFamily - local start script (double-click to run)
REM  First run: creates venv, installs dependencies, sets up DB.
REM  Every run: applies any pending DB migrations, starts server,
REM  opens your browser. Close this window to stop the app.
REM ============================================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found on PATH. Install from https://www.python.org/downloads/
    pause & exit /b 1
)

if not exist venv (
    echo [SETUP] Creating virtual environment...
    python -m venv venv || (pause & exit /b 1)
)

call venv\Scripts\activate.bat

echo [SETUP] Installing/updating dependencies (quick if unchanged)...
pip install -q -r requirements.txt || (pause & exit /b 1)

if not exist .env (
    echo [SETUP] No .env found - creating from .env.example. EDIT IT to add
    echo         your SECRET_KEY, Gmail app password and CAS password.
    copy .env.example .env >nul
)

REM Optional OCR dependencies (needed only for bank-statement PDF import)
where tesseract >nul 2>nul
if errorlevel 1 (
    echo [NOTE] Tesseract OCR not found - bank statement PDF import will not
    echo        work until you install it: https://github.com/UB-Mannheim/tesseract/wiki
    echo        ^(CAS import and NAV refresh work fine without it.^)
)
where pdftoppm >nul 2>nul
if errorlevel 1 (
    echo [NOTE] Poppler not found - also needed for bank statement import:
    echo        https://github.com/oschwartz10612/poppler-windows/releases
    echo        Unzip and add its Library\bin folder to PATH.
)

echo [DB] Applying database migrations...
set FLASK_APP=app.py
flask db upgrade || (echo [ERROR] Database migration failed & pause & exit /b 1)

echo [RUN] Starting FinFamily...
python serve.py
pause
