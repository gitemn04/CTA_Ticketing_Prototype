@echo off
REM CTA Metro v3.0 - Windows launcher
REM Double-click this file to start the application.

cd /d "%~dp0"

REM Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo Python is not installed or not on PATH.
    echo Download it from https://www.python.org/downloads/
    pause
    exit /b 1
)

REM Install / update dependencies silently if needed
echo Checking dependencies...
python -m pip install -q -r requirements.txt --break-system-packages 2>nul || python -m pip install -q -r requirements.txt

REM Launch the app
echo Starting CTA Metro...
python cta_app.py

pause
