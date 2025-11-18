@echo off
REM Run the app from command line (if using Python setup)

echo.
echo ==========================================
echo Starting Pflegedienst Invoicer...
echo ==========================================
echo.

REM Activate virtual environment
if exist .venv (
    call .venv\Scripts\activate.bat
)

REM Start the app
echo Starting server...
python -m uvicorn backend:app --host 127.0.0.1 --port 8000

echo.
echo Server stopped.
pause
