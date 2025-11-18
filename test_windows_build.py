#!/usr/bin/env python
"""
Test and build report generator for Windows executable.
Verifies build setup and outputs a report.

Usage:
  python test_windows_build.py
"""
import os
import sys
import subprocess
from pathlib import Path

def check_file_exists(path, description):
    """Check if a file exists."""
    exists = Path(path).exists()
    status = "✅" if exists else "❌"
    print(f"{status} {description}: {path}")
    return exists

def check_command(cmd, description):
    """Check if a command is available."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, shell=True)
        available = result.returncode == 0
        status = "✅" if available else "❌"
        print(f"{status} {description}")
        if available and result.stdout:
            print(f"   Version: {result.stdout.strip()}")
        return available
    except Exception as e:
        print(f"❌ {description}: {e}")
        return False

def main():
    print("\n" + "="*60)
    print("Windows Build Verification Report")
    print("="*60 + "\n")
    
    all_good = True
    
    # Check required files
    print("📋 Checking required files:")
    required_files = [
        ("pflegedienst_invoicer_windows.spec", "PyInstaller spec"),
        ("build_windows.bat", "Windows build script"),
        ("windows_launcher.py", "Windows launcher"),
        ("backend.py", "FastAPI app"),
        ("requirements.txt", "Dependencies"),
        ("README.md", "Documentation"),
        ("WINDOWS_INSTALLATION.md", "Windows guide"),
    ]
    
    for filepath, desc in required_files:
        if not check_file_exists(filepath, desc):
            all_good = False
    
    print("\n🔧 Checking tools:")
    
    # Check Python
    python_ok = check_command("python --version", "Python")
    if not python_ok:
        all_good = False
    
    # Check PyInstaller
    pyinstaller_ok = check_command("pyinstaller --version", "PyInstaller")
    if not pyinstaller_ok:
        print("\n⚠️  PyInstaller not found. Install with:")
        print("    pip install pyinstaller")
        all_good = False
    
    # Check pip packages
    print("\n📦 Checking Python packages:")
    packages = [
        "fastapi",
        "uvicorn",
        "pydantic",
        "jinja2",
        "weasyprint",
        "fitz",
        "pdfplumber",
        "PIL",
        "keyring",
        "dotenv",
    ]
    
    missing_packages = []
    for pkg in packages:
        try:
            __import__(pkg.replace('-', '_'))
            print(f"✅ {pkg}")
        except ImportError:
            print(f"❌ {pkg} (missing)")
            missing_packages.append(pkg)
            all_good = False
    
    if missing_packages:
        print(f"\n⚠️  Install missing packages with:")
        print(f"    pip install {' '.join(missing_packages)}")
    
    # Check database
    print("\n🗄️  Checking database:")
    db_path = Path("data/invoices.db")
    if db_path.exists():
        size_mb = db_path.stat().st_size / (1024*1024)
        print(f"✅ Database exists: {size_mb:.2f} MB")
    else:
        print(f"ℹ️  Database not found (will be created on first run)")
    
    # Check templates
    print("\n📄 Checking templates:")
    templates_path = Path("templates")
    if templates_path.exists():
        templates = list(templates_path.glob("*.html"))
        print(f"✅ Found {len(templates)} templates")
        for t in templates:
            print(f"   - {t.name}")
    else:
        print(f"❌ Templates folder not found")
        all_good = False
    
    # Check environment
    print("\n🔐 Checking environment setup:")
    env_path = Path(".env")
    env_sample_path = Path(".env.sample")
    
    if env_path.exists():
        print(f"✅ .env file exists (DO NOT COMMIT THIS!)")
    else:
        print(f"ℹ️  .env not found (normal before setup)")
    
    if env_sample_path.exists():
        print(f"✅ .env.sample template exists")
    else:
        print(f"❌ .env.sample template missing")
        all_good = False
    
    # Summary
    print("\n" + "="*60)
    if all_good:
        print("✅ All checks passed!")
        print("\nYou can now build the Windows executable:")
        print("    build_windows.bat")
        print("\nOr prepare for release:")
        print("    1. Install missing packages (if any)")
        print("    2. Run: build_windows.bat")
        print("    3. Test the .exe in dist/")
        print("    4. Package and send to customer")
    else:
        print("❌ Some issues found. Fix above before building.")
        print("\nCommon fixes:")
        print("    - Install PyInstaller: pip install pyinstaller")
        print("    - Install all dependencies: pip install -r requirements.txt")
        print("    - Create templates folder if missing")
    
    print("="*60 + "\n")
    
    return 0 if all_good else 1

if __name__ == "__main__":
    sys.exit(main())
