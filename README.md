# Pflegedienst Invoicer

A production-ready invoice generation system for German care facilities (Pflegedienste). Extracts billing data from PDFs, validates it, and generates professional invoices.

## Quick Start: Choose Your Installation Method

### 🪟 Windows Users (Recommended)

**No Python required!**

1. Download `Pflegedienst_Invoicer.exe`
2. Double-click to run
3. Paste your OpenAI API key when prompted
4. Done!

👉 See [WINDOWS_INSTALLATION.md](WINDOWS_INSTALLATION.md) for detailed instructions.

### 🍎 macOS Users

**Standalone app (no Python required):**

```bash
bash build_macos.sh
open "dist/Pflegedienst Invoicer.app"
```

Or use Python (see below).

### 🐧 Linux Users or Developers

See "Installation (Python)" section below.

## Features

- **PDF Parsing**: Extract billing data from supplier PDFs using AI (OpenAI)
- **Invoice Generation**: Create professional PDF invoices from structured data
- **Database**: SQLite (local) with optional PostgreSQL support for multi-user deployments
- **REST API**: FastAPI backend with CORS support for local installers
- **Web UI**: Compatible with Tauri and other desktop frameworks
- **Logging**: Structured logging with secure key management
- **Desktop App**: Standalone apps for Windows (.exe), macOS (.app), and Python-based Linux

---

## Installation (Python - for developers or advanced users)

1. **Clone or extract the project**:
   ```bash
   cd pflegedienst_invoicer
   ```

2. **Create and activate a virtual environment** (recommended):
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Set your OpenAI API key**:
   ```bash
   python manage_secrets.py set
   ```
   Follow the interactive prompt to paste your key. It will be stored securely in your OS keyring or in `.env`.

   Alternatively, create a `.env` file (from `.env.sample`):
   ```bash
   cp .env.sample .env
   # Edit .env and add your OpenAI API key
   ```

5. **Run the backend API**:
   ```bash
   uvicorn backend:app --reload
   ```
   API available at `http://localhost:8000`

6. **(Optional) Run the CLI version**:
   ```bash
   python main_terminal_version.py
   ```
   While the CLI menu is running, option `7` opens the new web-based RAG dashboard (`GET /dashboard`) in your default browser (make sure the FastAPI backend is already running).

### Setting up for the first time

1. Place your supplier PDFs in the `data/abrechnung/` folder
2. Use the API (`/upload_pdf`, `/prepare_pdf`, `/process_pdf`) or CLI to process them
3. Generate invoices with `/generate_invoices` or the CLI

## Usage

### REST API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/upload_pdf` | Upload a PDF file |
| POST | `/prepare_pdf` | Extract and chunk PDF text |
| POST | `/process_pdf` | Process chunks with OpenAI and import to DB |
| POST | `/generate_invoices` | Generate invoice PDFs for a month |
| POST | `/complete_data` | Fill in missing patient fields |
| POST | `/check_service_fields` | Validate service line items |
| POST | `/retry_failed` | Reprocess failed chunks |
| POST | `/regenerate_invoice` | Regenerate a single invoice |
| POST | `/mark_ready` | Mark invoices as ready for generation |
| GET | `/logs/stream` | Stream processing logs (SSE) |
| POST | `/ai/visualize` | Generate charts from natural-language questions |
| POST | `/agent/query` | Agent that fetches RAG evidence and answers structured questions |

Example:
```bash
curl -X POST http://localhost:8000/generate_invoices \
  -H "Content-Type: application/json" \
  -d '{"abrechnungsmonat": "092025"}'
```

### Managing Secrets

**Set OpenAI API key interactively**:
```bash
python manage_secrets.py set
```

**Delete key from keyring** (if needed):
```bash
python manage_secrets.py delete
```

The app checks for the key in this order:
1. `OPENAI_API_KEY` environment variable
2. OS keyring (macOS Keychain, Windows Credential Manager, etc.)
3. `.env` file

## Configuration

Create or edit `.env` in the project root:
```ini
# OpenAI API Key (required)
OPENAI_API_KEY=sk-...

# Database path (optional, defaults to data/invoices.db)
DB_PATH=data/invoices.db

# Log level (optional, defaults to INFO)
LOG_LEVEL=INFO
```

**Important**: Never commit `.env` to version control. Use `.env.sample` as a template.

## Database

### Local (SQLite)

By default, the app uses SQLite at `data/invoices.db`. This is fine for single-user/local installs.

- WAL mode is enabled automatically for better concurrency
- Connection timeout is set to 5 seconds
- Foreign keys are enforced

### Production (PostgreSQL)

For multi-user or server deployments, PostgreSQL is recommended. Future versions will support easy migration.

## Building the macOS App

To create a standalone app bundle for distribution:

1. **Install PyInstaller**:
   ```bash
   pip install pyinstaller
   ```

2. **Run the build script**:
   ```bash
   bash build_macos.sh
   ```

3. **Find the app** in `dist/Pflegedienst Invoicer.app`

4. **(Optional) Create a DMG installer**:
   ```bash
   hdiutil create -volname 'Pflegedienst Invoicer' -srcfolder dist -ov -format UDZO dist/Pflegedienst_Invoicer.dmg
   ```

5. **Distribute** the `.dmg` or `.app` to customers

## Logging

Logs are written to stdout and can be streamed via the `/logs/stream` endpoint (SSE).

Log level is controlled by the `LOG_LEVEL` environment variable:
- `DEBUG`: Detailed information for debugging
- `INFO`: General informational messages (default)
- `WARNING`: Warning messages
- `ERROR`: Error messages

## Troubleshooting

### "OPENAI_API_KEY is not set"

Solution: Set your OpenAI API key using:
```bash
python manage_secrets.py set
```

Or set the environment variable:
```bash
export OPENAI_API_KEY=sk-...
```

### "Database is locked"

The app retries locked database operations automatically. If this persists:
1. Ensure no other processes are accessing `data/invoices.db`
2. Try restarting the app
3. Delete `.db-wal` and `.db-shm` files if they exist (only when app is stopped)

### PDF extraction fails

- Check that the PDF format is supported (text extraction from scanned PDFs may require OCR)
- Ensure your OpenAI API key is valid and has sufficient credits
- Check logs with `LOG_LEVEL=DEBUG`

### Keyring issues on macOS

On older macOS versions or in headless environments, keyring may not work. The app will fall back to `.env` automatically.

To force `.env` usage, delete the key from keyring:
```bash
python manage_secrets.py delete
```

## Development

### Project Structure

```
pflegedienst_invoicer/
├── app/
│   ├── cli/              # CLI tools (secrets management)
│   ├── core/             # Config, logging
│   ├── db/               # Database utilities
│   ├── database.py       # SQLite ORM-like operations
│   ├── invoice_generator.py
│   ├── pdf_parser.py
│   ├── openai_client.py
│   └── ai_schema.py
├── templates/            # Jinja2 invoice templates
├── data/
│   ├── abrechnung/       # Input PDFs
│   └── invoices.db       # SQLite database
├── output/
│   └── invoices/         # Generated PDFs
├── logs/
├── backend.py            # FastAPI app
├── main_terminal_version.py  # CLI runner
├── manage_secrets.py     # Secrets management entry point
├── requirements.txt      # Dependencies
└── .env.sample          # Environment template
```

### Running Tests (Future)

```bash
pytest tests/
```
Example: `pytest tests/test_rag_agent.py` now validates the RAG chunk metadata/agent pipeline.

### Code Style

Install dev dependencies:
```bash
pip install black flake8 isort
```

Format code:
```bash
black app/ backend.py
isort app/ backend.py
```

Check style:
```bash
flake8 app/ backend.py
```

## Security

- **API Keys**: Stored securely in OS keyring or `.env` (never in source code)
- **Database**: Foreign keys enforced, input validated
- **Logging**: Secrets are not logged
- **Secrets scanning**: Use `detect-secrets` before committing

## Performance

- **SQLite**: Optimized with WAL mode, 64MB cache, and connection pooling
- **PDF Processing**: Chunks are cached during a session
- **OpenAI Calls**: Retries and backoff built in
- **Logging**: Minimal overhead with structured logging

## Phase 4 — PDF Parsing & OpenAI Integration

Phase 4 focused on improving PDF parsing reliability and making OpenAI usage more efficient and predictable.

Key changes in Phase 4:

- Centralized parsing configuration in `app/config/parsing.py` with environment overrides (`PARSING_MIN_AMOUNT`, `PARSING_QUANTITY_TOLERANCE`, `PARSING_MAX_TOKENS_PER_CALL`).
- Token counting utility in `app/openai_utils.py` using `tiktoken` when available with a safe heuristic fallback.
- File-based response cache (`cache/openai/`) to avoid duplicate OpenAI calls and reduce cost.
- `app/pdf_parser.py` updated to split chunks by token estimates so prompts stay within conservative model limits.
- Unit tests added for parsing, patterns, token counting, caching and chunk splitting (see `tests/`).

Why it matters:

- Reduces OpenAI API costs and repeated calls.
- Prevents "context too long" errors by proactively splitting large chunks.
- Makes parsing behavior configurable and easier to tune per customer/document.

See `ARCHITECTURE.md` and `PHASE4_COMPLETION_REPORT.md` for details, tests, and recommendations.

## Support & Contributions

For issues or feature requests, please contact the developer.

## License

Proprietary. Do not distribute without permission.

---

**Version**: 1.0.0  
**Last Updated**: November 2025
