# Windows Installation Guide

## For Customers (No Python Required)

### Option 1: Run the Executable (Easiest)

Your first Windows customer just needs to:

1. **Download** `Pflegedienst_Invoicer.exe`
2. **Double-click** the .exe file
3. **On first run**: Paste your OpenAI API key when prompted
4. **Done!** The app opens automatically in your browser

That's it. No Python, no command line, no setup.

---

## For Building the Windows Executable

### Prerequisites (on your machine)

- Python 3.9+ installed
- PyInstaller: `pip install pyinstaller`

### Build Steps

```bash
# Navigate to project directory
cd pflegedienst_invoicer

# Create virtual environment (optional but recommended)
python -m venv .venv
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Build the Windows executable
build_windows.bat
```

**Result**: `dist\Pflegedienst_Invoicer.exe` (ready to distribute)

---

## How It Works

1. **User runs the .exe** → Windows Launcher starts
2. **Launcher checks** for API key configuration
3. **First time?** → Prompts user to paste OpenAI API key
4. **Launcher starts** the FastAPI backend (http://127.0.0.1:8000)
5. **Browser opens** automatically to the app
6. **User starts** processing invoices

---

## What the .exe Contains

- ✅ Python runtime (no external Python needed)
- ✅ All required libraries bundled
- ✅ FastAPI backend
- ✅ Invoice templates
- ✅ Database initialization
- ✅ Settings (.env.sample)

**Total size**: ~300-400 MB (typical for bundled Python apps)

---

## For Your Customer (Detailed Instructions)

Create this instruction sheet and send with the .exe:

---

### 📖 Pflegedienst Invoicer - Installation & Setup (Windows)

**Installation**:
1. Download `Pflegedienst_Invoicer.exe` from [your website/email]
2. Double-click the .exe file to run it
3. Windows may warn "Unknown publisher" — click "Run anyway"

**First-Time Setup**:
1. A window opens with setup instructions
2. You'll need your OpenAI API key:
   - Get one free at: https://platform.openai.com/api-keys
   - (Sign up for OpenAI account if you don't have one)
   - Create a new API key
3. Paste the key when prompted (text won't appear on screen for security)
4. Click Enter

**Using the App**:
1. A browser window opens automatically
2. You can now:
   - Upload PDFs from your supplier
   - Process invoices
   - Generate PDFs
   - Check data

**Important Notes**:
- Your API key is stored locally on your computer (in `C:\Users\<YourName>\AppData\Local\Pflegedienst Invoicer\.env`)
- It is NOT sent to anyone
- Keep it secret (like a password)
- You pay OpenAI only for what you use (usually a few cents per invoice)

**Troubleshooting**:
- **Port 8000 already in use**: Close other apps using it, then restart
- **"Unknown publisher" warning**: This is normal for unsigned .exe files; click "Run anyway"
- **API key not working**: Check it's valid at https://platform.openai.com/api-keys
- **Logs**: Check `invoicer_launcher.log` in the same folder as the .exe

**Support**: Contact [your email] for help

---

## Advanced: Creating a Windows Installer

If you want to distribute as an installer (.exe or .msi), use one of these tools:

### Option A: NSIS (Nullsoft Installer System) - Recommended

1. Install NSIS: https://nsis.sourceforge.io/
2. Create an NSIS script to wrap `Pflegedienst_Invoicer.exe`
3. Produces a small `.exe` installer that unpacks the app

### Option B: InnoSetup

1. Install InnoSetup: https://jrsoftware.org/isdl.php
2. Create an InnoSetup script
3. Produces a professional Windows installer

### Option C: WiX (Windows Installer XML) - Enterprise

For production-grade .msi installers.

---

## Keeping the App Updated

1. Build a new version: `build_windows.bat`
2. Send updated `.exe` to customers
3. They can install it alongside the old version (or replace it)

---

## Performance Notes

- **First launch**: May take 10-20 seconds (Python runtime extracts)
- **Subsequent launches**: 3-5 seconds
- **API calls**: Depends on PDF size and internet speed
- **Storage**: App uses ~100MB for database + generated PDFs (grows with usage)

---

## Files Generated

When your customer runs the app:

```
C:\Users\<YourName>\AppData\Local\Pflegedienst Invoicer\
├── .env                 (API key stored here - KEEP PRIVATE)
├── data/
│   ├── invoices.db      (invoice database)
│   └── abrechnung/      (uploaded PDFs)
└── output/
    └── invoices/        (generated invoice PDFs)
```

---

## Frequently Asked Questions

**Q: Is my API key safe?**  
A: Yes. Your key is stored on YOUR computer in `AppData\Local`. It never goes to our servers.

**Q: What if I lose my API key?**  
A: Create a new one at https://platform.openai.com/api-keys and run the app again — it will prompt you to set it up.

**Q: Can multiple people use the app?**  
A: Not yet. This version runs locally on one computer. Future versions will support multiple users.

**Q: Do I need internet?**  
A: Yes, for OpenAI API calls. Local database operations work offline, but processing PDFs requires internet.

**Q: Can I uninstall it?**  
A: Yes, just delete the .exe. Your data remains in `AppData\Local\Pflegedienst Invoicer` (you can back it up or delete it).

---

## For Developers: Rebuilding

To rebuild the .exe after code changes:

```bash
cd pflegedienst_invoicer
pip install -r requirements.txt
build_windows.bat
```

Check `dist\Pflegedienst_Invoicer.exe`

---

That's it! Your Windows customer has a fully functional, standalone invoicer app.

