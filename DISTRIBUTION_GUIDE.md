# Distribution Guide

## For Your Customer

Choose the right distribution based on their operating system:

### Windows Customer (No Python)

**What to send:**
- `Pflegedienst_Invoicer.exe` file
- `WINDOWS_INSTALLATION.md` (instructions)

**Customer experience:**
1. Downloads .exe
2. Double-clicks it
3. Pastes API key
4. App runs in browser
5. Done!

**You build it:**
```bash
build_windows.bat
# Creates: dist\Pflegedienst_Invoicer.exe
```

---

### macOS Customer (No Python)

**What to send:**
- `Pflegedienst Invoicer.app` (bundled app)
- `README.md` (instructions)
- Optionally: `Pflegedienst_Invoicer.dmg` (installer)

**Customer experience:**
1. Downloads .dmg or .app
2. Opens in Finder
3. Runs app
4. Pastes API key
5. App runs in browser
6. Done!

**You build it:**
```bash
bash build_macos.sh
# Creates: dist/Pflegedienst Invoicer.app
```

---

### Technical Customer (Has Python)

**What to send:**
- Source code (git clone or .zip)
- `requirements.txt`
- `QUICK_START.md` (instructions)

**Customer does:**
```bash
git clone <repo> && cd pflegedienst_invoicer
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
python manage_secrets.py set
uvicorn backend:app --reload
```

---

## Your Distribution Workflow

### For Each Release

```bash
# 1. Update version in code (optional)
# 2. Test locally
# 3. Build Windows executable
build_windows.bat
# → dist\Pflegedienst_Invoicer.exe

# 4. Build macOS app
bash build_macos.sh
# → dist/Pflegedienst Invoicer.app

# 5. Create macOS installer (optional)
hdiutil create -volname 'Pflegedienst Invoicer' \
  -srcfolder dist -ov -format UDZO \
  dist/Pflegedienst_Invoicer.dmg

# 6. Package and distribute
# - Windows: Pflegedienst_Invoicer.exe
# - macOS: Pflegedienst Invoicer.app or .dmg
# - Technical: source code + QUICK_START.md
```

---

## File Manifest for Windows Customer

Send these files in a folder or zip:

```
Pflegedienst_Invoicer.exe          (the app)
WINDOWS_INSTALLATION.md            (setup guide)
README.md                          (general info)
```

---

## File Manifest for macOS Customer

Send one of:

**Option A: Standalone app (simple)**
```
Pflegedienst Invoicer.app/         (the app directory)
README.md                          (instructions)
```

**Option B: Installer (drag-and-drop)**
```
Pflegedienst_Invoicer.dmg          (installer)
README.md                          (instructions)
```

---

## File Manifest for Technical Customer

```
pflegedienst_invoicer/             (entire source code)
├── app/
├── templates/
├── data/
├── backend.py
├── main_terminal_version.py
├── requirements.txt
├── QUICK_START.md
└── ... (all other files)
```

---

## Testing Each Build

### Windows .exe Test

```cmd
# Download/receive the .exe
Pflegedienst_Invoicer.exe
# Should:
# 1. Open without errors
# 2. Prompt for API key (first time)
# 3. Start server and open browser
# 4. Show API running message
```

### macOS .app Test

```bash
open Pflegedienst\ Invoicer.app
# Should:
# 1. Open and run
# 2. Prompt for API key (first time)
# 3. Open browser automatically
```

### macOS .dmg Test

```bash
# Mount .dmg
open Pflegedienst_Invoicer.dmg
# User sees installer window
# Can drag app to Applications folder
# Launch from Applications
```

---

## Versioning

Recommended approach:

```
Version 1.0.0
├── Pflegedienst_Invoicer_v1.0.0.exe
├── Pflegedienst_Invoicer_v1.0.0.dmg
└── CHANGELOG.md
```

Include in CHANGELOG:
- Bug fixes
- New features
- API key update instructions (if needed)

---

## Hosting/Distribution Services

Options for hosting installers:

1. **GitHub Releases** (free)
   - Upload .exe, .dmg, source code
   - Public or private releases
   - Supports versioning and notes

2. **Dropbox/Google Drive** (free)
   - Simple file sharing
   - Less professional but easy

3. **AWS S3/Azure Blob** (cheap)
   - Scalable, CDN support
   - Pay per download

4. **Your website** (if you have one)
   - Host .exe and .dmg directly
   - Professional appearance

---

## Customer Support

When customer reports issues:

1. **"App won't start"**
   - Check Windows/macOS version compatibility
   - Ensure they pasted API key correctly
   - Check internet connection

2. **"API key not working"**
   - Verify key at https://platform.openai.com/api-keys
   - Check if billing is enabled on OpenAI account
   - Try generating new key

3. **"Port 8000 already in use"**
   - Another app is using it
   - Ask them to restart computer or close other apps

4. **Performance issues**
   - Check PDF file size
   - Check internet speed (OpenAI API calls)
   - Database may need cleanup (delete old invoices)

---

## Security Notes for Distribution

- ✅ API key is NOT embedded in the .exe
- ✅ Each customer has their own key (managed locally)
- ✅ No data sent to your servers (local only)
- ✅ Safe to distribute (no secrets in code)

Before distributing:
- [ ] Rotated the exposed API key from source code
- [ ] Tested .exe works on Windows
- [ ] Tested .app works on macOS
- [ ] Created WINDOWS_INSTALLATION.md
- [ ] README.md is complete
- [ ] QUICK_START.md is ready

---

## Rollback Plan

If a release has bugs:

1. Keep previous version .exe/.dmg
2. Tell customers to use previous version
3. Fix the bug
4. Release new version
5. Include migration notes if needed

---

**You're ready to distribute!** 🎉

