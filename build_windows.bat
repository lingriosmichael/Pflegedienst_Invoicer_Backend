@echo off
REM Build script for Windows standalone executable using PyInstaller
REM Usage: build_windows.bat

echo.
echo ==========================================
echo Building Pflegedienst Invoicer for Windows...
echo ==========================================
echo.

REM Check if PyInstaller is installed
pyinstaller --version >nul 2>&1
if errorlevel 1 (
    echo Error: PyInstaller not found. Install with:
    echo   pip install pyinstaller
    pause
    exit /b 1
)

REM Clean previous builds
echo Cleaning previous builds...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "dist\Pflegedienst_Invoicer" rmdir /s /q "dist\Pflegedienst_Invoicer"

REM Run PyInstaller
echo.
echo Running PyInstaller...
pyinstaller pflegedienst_invoicer_windows.spec

REM Check if build succeeded
if not exist "dist\Pflegedienst_Invoicer.exe" (
    echo.
    echo Error: Build failed. Check the output above.
    pause
    exit /b 1
)

echo.
echo ==========================================
echo Build complete!
echo ==========================================
echo.
echo Executable created: dist\Pflegedienst_Invoicer.exe
echo.
echo Next steps:
echo   1. Run the .exe (no Python required)
echo   2. On first launch, you'll be prompted for your OpenAI API key
echo   3. Distribute the exe to your customers
echo.
echo To create a Windows installer (.msi or .exe installer):
echo   - Use NSIS (nullsoft installer system) or InnoSetup
echo   - Or distribute the .exe directly
echo.
pause
