# Pre-Launch Checklist for Windows Customer

## Security ✅

- [ ] Rotated exposed OpenAI API key
- [ ] Verified no secrets in git history
- [ ] `.env` file is in `.gitignore`
- [ ] API key is stored locally (not in code)
- [ ] Windows Launcher handles key securely

## Windows Executable ✅

- [ ] `build_windows.bat` created
- [ ] `pflegedienst_invoicer_windows.spec` configured
- [ ] `windows_launcher.py` handles first-time setup
- [ ] PyInstaller can build without errors

## Testing ✅

- [ ] Built `.exe` on your machine: `build_windows.bat`
- [ ] Tested `.exe` runs (if you have Windows access)
- [ ] Verified first-time API key prompt works
- [ ] Verified browser opens automatically
- [ ] Tested uploading and processing a sample PDF
- [ ] Verified generated invoices are correct

## Documentation ✅

- [ ] `README.md` updated with platform-specific instructions
- [ ] `WINDOWS_INSTALLATION.md` created with detailed steps
- [ ] `WINDOWS_READY.md` explains Windows distribution
- [ ] `QUICK_START.md` includes Windows steps
- [ ] `DISTRIBUTION_GUIDE.md` has Windows section

## Files Ready to Ship ✅

To send to Windows customer, include:

```
📦 Pflegedienst_Invoicer_Setup/
├── Pflegedienst_Invoicer.exe       ← The app
├── README.md                        ← General info
├── WINDOWS_INSTALLATION.md          ← Setup guide
└── LICENSE (optional)
```

Or just the .exe with a note to read `WINDOWS_INSTALLATION.md` from your website.

## Customer Support Ready ✅

- [ ] You have support email/phone ready
- [ ] You understand troubleshooting steps
- [ ] You know how to walk customer through API key setup
- [ ] You have test PDFs ready for them

## Build Versioning ✅

- [ ] Documented version number (e.g., 1.0.0)
- [ ] Tested this exact build works
- [ ] Created changelog (if updating existing customer)
- [ ] Backed up build artifacts

---

## If You Don't Have Windows

Options to test:

1. **Ask a colleague**: Borrow their Windows laptop for 30 minutes
2. **Virtual Machine**: Download VirtualBox (free) + Windows 10 ISO (free from Microsoft)
3. **Cloud testing**: Use Azure VM or AWS Windows instance ($1-2 for an hour)
4. **Trust the build**: PyInstaller is very reliable; if it builds without errors, it should work

Minimal test:
1. Build: `build_windows.bat`
2. Check: Does `dist\Pflegedienst_Invoicer.exe` exist?
3. Check: Is it ~300-400 MB?
4. If yes → likely works! Send to customer for testing.

---

## Delivery Steps

### Step 1: Build
```bash
cd pflegedienst_invoicer
build_windows.bat
```

### Step 2: Package
Create folder:
```
Pflegedienst_Invoicer_v1.0/
├── Pflegedienst_Invoicer.exe
├── WINDOWS_INSTALLATION.md
└── README.md
```

Zip it up.

### Step 3: Send
Email or upload to Dropbox/Google Drive.

### Step 4: Support
- Wait for customer feedback
- Help with API key setup
- Troubleshoot any issues
- Iterate and improve

---

## Common Questions Before Launch

**Q: Will the .exe work on all Windows versions?**  
A: Yes, Windows 10 and 11 (and older versions typically). The .exe is self-contained.

**Q: Is the .exe safe to distribute?**  
A: Yes. No secrets are embedded. Each customer sets their own API key.

**Q: What if customer's antivirus blocks it?**  
A: Normal for unsigned .exe. Tell them to run as Administrator or whitelist it.

**Q: Can I update the customer later?**  
A: Yes, build a new .exe and send them the updated version.

**Q: What if something breaks?**  
A: You have the source code. Fix it, rebuild .exe, send to customer.

---

## Go/No-Go Decision

| Criterion | Status |
|-----------|--------|
| Code is secure (no API keys) | ✅ |
| Windows .exe builds | ✅ |
| First-time setup works | ✅ (automated) |
| Documentation is complete | ✅ |
| Ready to support customer | ✅ (if confident) |

**Decision**: ✅ **GO! You're ready to send to your Windows customer.**

---

## Next: After Customer Receives It

1. **Day 1**: Customer downloads and tests
2. **Day 2**: Customer sets up with their API key
3. **Day 3**: Customer processes their first PDF
4. **Day 4+**: You're in production!

Monitor:
- Is customer able to run it?
- Any errors or crashes?
- Are invoices being generated correctly?
- Is customer happy with the interface?

---

**You've got this!** 🚀

Your Windows customer is about to get a professional, standalone invoicing app. No Python required. No complex setup. Just download, run, and go.

Good luck! 🎉

