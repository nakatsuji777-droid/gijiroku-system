@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo ========================================
echo   Gijiroku System Starting...
echo ========================================
echo.

REM Create venv if not exists
if not exist ".venv" (
    echo [1/3] Creating Python virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo [ERROR] Python is not installed or not in PATH.
        pause
        exit /b 1
    )
)

REM Activate venv
call .venv\Scripts\activate.bat

REM Install dependencies if needed
if not exist ".venv\Lib\site-packages\google\genai" (
    echo [2/3] Installing dependencies...
    pip install --upgrade pip
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
)

REM Check .env
if not exist ".env" (
    echo.
    echo [WARNING] .env file not found.
    echo Please set GOOGLE_API_KEY in .env file.
    echo Get it from: https://aistudio.google.com/apikey
    echo.
    pause
)

REM Start Streamlit
echo [3/3] Starting app...
echo.
echo Open http://localhost:8501 in your browser.
echo Press Ctrl+C to stop.
echo.

streamlit run app.py

pause