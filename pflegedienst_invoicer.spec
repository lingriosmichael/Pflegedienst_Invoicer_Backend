# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for pflegedienst_invoicer.
Builds a standalone macOS app bundle.

Usage:
  pyinstaller pflegedienst_invoicer.spec

This creates a distributable .app bundle in dist/ folder.
Requires: pyinstaller, pip install pyinstaller
"""

import os
import sys

block_cipher = None

a = Analysis(
    ['backend.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('.env.sample', '.'),
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
    name='pflegedienst_invoicer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # No console window on macOS
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

app = BUNDLE(
    exe,
    name='Pflegedienst Invoicer.app',
    icon=None,  # Add your icon.icns path here
    bundle_identifier='com.michaelfernando.pflegedienst-invoicer',
    info_plist={
        'NSPrincipalClass': 'NSApplication',
        'CFBundleInfoDictionaryVersion': '6.0',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0.0',
    },
)
