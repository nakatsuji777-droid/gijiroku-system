@echo off
cd /d "%~dp0"

echo ========================================
echo   Gijiroku System - Starting...
echo ========================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating Python virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Python is not installed.
        pause
        exit /b 1
    )
)

if not exist ".venv\Lib\site-packages\streamlit" (
    echo [2/3] Installing packages...
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\python.exe -m pip install -r requirements-local.txt
    if errorlevel 1 (
        echo [ERROR] Package install failed.
        pause
        exit /b 1
    )
)

if not exist ".env" (
    echo [WARNING] .env file not found.
    echo Please set GOOGLE_API_KEY in .env file.
    pause
)

echo [3/3] Starting app...
echo.
echo Close this window to stop.
echo.

start "" http://localhost:8501
.venv\Scripts\python.exe -m streamlit run app.py

pause
