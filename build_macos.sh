#!/bin/bash
# Build script for macOS standalone app using PyInstaller
# Usage: bash build_macos.sh

set -e

echo "🔨 Building Pflegedienst Invoicer for macOS..."

# Check if PyInstaller is installed
if ! command -v pyinstaller &> /dev/null; then
    echo "❌ PyInstaller not found. Install with: pip install pyinstaller"
    exit 1
fi

# Clean previous builds
echo "🧹 Cleaning previous builds..."
rm -rf build dist *.dmg

# Run PyInstaller
echo "⚙️  Running PyInstaller..."
pyinstaller pflegedienst_invoicer.spec

# Check if build succeeded
if [ ! -d "dist/Pflegedienst Invoicer.app" ]; then
    echo "❌ Build failed."
    exit 1
fi

echo "✅ Build complete! App created: dist/Pflegedienst Invoicer.app"
echo ""
echo "📦 To distribute:"
echo "   1. Code sign the app (optional but recommended):"
echo "      codesign -s - dist/Pflegedienst\ Invoicer.app"
echo ""
echo "   2. Create a DMG (drag-and-drop installer):"
echo "      hdiutil create -volname 'Pflegedienst Invoicer' -srcfolder dist -ov -format UDZO dist/Pflegedienst_Invoicer.dmg"
echo ""
echo "   3. Distribute the .dmg file to customers"
