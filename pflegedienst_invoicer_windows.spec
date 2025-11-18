# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for pflegedienst_invoicer (Windows).
Builds a standalone .exe with all dependencies bundled.

Usage:
  pyinstaller pflegedienst_invoicer_windows.spec

This creates a distributable .exe in dist/ folder.
Requires: pyinstaller
  pip install pyinstaller
"""

import os
import sys

block_cipher = None

a = Analysis(
    ['windows_launcher.py'],  # Use the launcher as entry point
    pathex=[],
    binaries=[
        # WeasyPrint requires these system libraries on Windows
        # They should be bundled by the weasyprint package, but add them if needed
    ],
    datas=[
        ('templates', 'templates'),
        ('.env.sample', '.'),
        ('data', 'data'),
    ],
    hiddenimports=[
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websocket',
        'uvicorn.protocols.websocket.auto',
        'uvicorn.server',
        'uvicorn.workers',
        'fastapi',
        'pydantic',
        'jinja2',
        'weasyprint',
        'fitz',
        'pdfplumber',
        'pytesseract',
        'PIL',
        'keyring',
        'keyring.backends',
        'keyring.backends.Windows',
        'dotenv',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludedimports=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(
    a.pure,
    a.zipped_data,
    cipher=block_cipher,
)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Pflegedienst_Invoicer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # No console window (use False for GUI app, True for CLI)
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # Add your .ico file path here if you have one
)

# Optional: Create a Windows installer (.msi) using pyinstaller
# Requires additional setup, but the .exe can be distributed standalone
