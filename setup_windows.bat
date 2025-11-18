@echo off
REM Quick setup script for Windows - runs if user has Python installed
REM This is optional - the .exe approach is preferred

echo.
echo ==========================================
echo Pflegedienst Invoicer - Quick Setup
echo ==========================================
echo.
echo This script sets up the app if you have Python installed.
echo If you don't have Python, use the Pflegedienst_Invoicer.exe instead.
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo Error: Python not found. Please:
    echo   Option 1: Download Python from https://www.python.org
    echo   Option 2: Use the Pflegedienst_Invoicer.exe instead (no Python needed)
    pause
    exit /b 1
)

echo ✅ Python found!
echo.

REM Create virtual environment
echo Creating virtual environment...
if not exist .venv (
    python -m venv .venv
)

REM Activate virtual environment
echo Activating virtual environment...
call .venv\Scripts\activate.bat

REM Install dependencies
echo Installing dependencies...
pip install -r requirements.txt

REM Set API key
echo.
echo ==========================================
echo Setting up your OpenAI API Key
echo ==========================================
echo.
echo Go to: https://platform.openai.com/api-keys
echo Create a new API key and paste it below.
echo.

python manage_secrets.py set

echo.
echo ==========================================
echo Setup Complete!
echo ==========================================
echo.
echo To start the app, run:
echo   run_app.bat
echo.
pause
