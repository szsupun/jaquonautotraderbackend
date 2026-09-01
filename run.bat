@echo off
echo ============================================================
echo    POCKET OPTION AUTO TRADER - BACKEND
echo    Telegram bot + trading loop + Mini App API
echo    (frontend is deployed separately, e.g. to Vercel)
echo ============================================================
echo.

REM Check if Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH!
    echo Install Python from https://python.org
    pause
    exit /b 1
)

REM Install dependencies if needed
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
    call venv\Scripts\activate.bat
    echo Installing dependencies...
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate.bat
    echo Checking for new dependencies...
    pip install -r requirements.txt -q
)

echo.
echo Starting bot + API... Use /start in Telegram to begin trading.
echo Mini App API: http://localhost:8001  (frontend calls this URL)
echo Press Ctrl+C to stop.
echo.

python main.py

echo.
echo Bot stopped.
pause
