from fastapi import FastAPI, UploadFile, File, Form, Request
from pydantic import BaseModel
from typing import List, Optional
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi import HTTPException
import traceback
import shutil
import os
import asyncio
import app.database as database
import app.invoice_generator as invoice_generator
import app.pdf_parser as pdf_parser
from app.db import create_collections_and_indexes
from app.db.mongodb_config import health_check as mongodb_health_check
from app.core import auth as auth_core
from app.entlastung_balance import (
    uses_new_entlastung_logic,
    recompute_entlastung_for_care_event,
    seed_entlastung_2026_balances,
)
import uuid
from datetime import datetime
import logging
from app.core.logging import setup_logging, get_logger

logger = get_logger(__name__)
setup_logging()

# Paths that must remain reachable without a session token.
PUBLIC_PATHS = {"/health", "/auth/login"}

app = FastAPI()


@app.middleware("http")
async def require_auth(request: Request, call_next):
    if request.method == "OPTIONS" or request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"detail": "Missing bearer token"})

    token = auth_header[len("Bearer "):]
    try:
        auth_core.decode_access_token(token)
    except auth_core.InvalidTokenError:
        return JSONResponse(status_code=401, content={"detail": "Invalid or expired session"})

    return await call_next(request)


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
def login(req: LoginRequest):
    try:
        auth_core.verify_login(req.username, req.password)
    except auth_core.InvalidCredentialsError:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = auth_core.create_access_token(req.username)
    return {"access_token": token, "token_type": "bearer"}


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:1420",
        "http://127.0.0.1:1420",
        "tauri://localhost",
        "http://localhost:5173",      # Vite dev server
        "http://127.0.0.1:5173",      # Vite dev server
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")

UPLOAD_DIR = "data/abrechnung"

def _prepare_chunks_for_file(file_name: str, abrechnungsmonat: str):
    """Extract and cache PDF chunks for processing."""
    path = os.path.join(UPLOAD_DIR, file_name)
    text = pdf_parser.extract_text_from_pdf(path)
    
    # Extract billing summary from first page
    first_page_text = pdf_parser.extract_first_page_text(path)
    billing_summary = pdf_parser.extract_billing_summary(first_page_text)
    if billing_summary:
        database.insert_billing_summary(billing_summary, abrechnungsmonat)
    
    raw_chunks = pdf_parser.split_into_chunks(text)
    now_ts = datetime.now().isoformat()

    chunk_entries = []
    for chunk_text in raw_chunks:
        cid = str(uuid.uuid4())
        chunk_entry = {
            "chunk_id": cid,
            "text": chunk_text,
            "source_pdf": file_name,
            "patient_name": None,
            "created_at": now_ts,
        }
        chunk_entries.append(chunk_entry)

    pdf_parser.store_chunks_temp(abrechnungsmonat, chunk_entries)
    return chunk_entries

# ------------------------------
# Request Models
# ------------------------------
class ProcessRequest(BaseModel):
    file_name: str
    abrechnungsmonat: str
    mode: str   # "sgbxi", "sgbv", "verhinderungspflege", or "entleistung"

class InvoiceRequest(BaseModel):
    abrechnungsmonat: str


# ------------------------------
# Startup
# ------------------------------
@app.on_event("startup")
async def startup_event():
    try:
        # Initialize MongoDB collections and indexes
        logger.info("Initializing MongoDB...")
        create_collections_and_indexes()
        logger.info("✓ MongoDB collections initialized")

        # Seed the 2026 Entlastungsleistung bucket for any patient that doesn't
        # have one yet (idempotent — never touches an existing row).
        seed_summary = seed_entlastung_2026_balances()
        logger.info(f"✓ Entlastungsleistung 2026 balances: {seed_summary}")

    except Exception as e:
        logger.error(f"Failed to init database: {e}")


# ------------------------------
# Endpoints
# ------------------------------
@app.get("/health")
def health():
    mongo_ok = mongodb_health_check()
    return JSONResponse(
        status_code=200 if mongo_ok else 503,
        content={"status": "ok" if mongo_ok else "degraded", "mongodb": mongo_ok},
    )


def _safe_upload_filename(original_filename: str) -> str:
    """Build a filesystem-safe filename that cannot escape UPLOAD_DIR."""
    base_name = os.path.basename(original_filename or "")
    if not base_name.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only .pdf files are accepted")
    safe_stem = "".join(c for c in os.path.splitext(base_name)[0] if c.isalnum() or c in ("-", "_")) or "upload"
    return f"{safe_stem}_{uuid.uuid4().hex[:8]}.pdf"


@app.post("/upload_pdf")
def upload_pdf(file: UploadFile = File(...), abrechnungsmonat: str = Form(...)):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    safe_filename = _safe_upload_filename(file.filename)
    file_path = os.path.join(UPLOAD_DIR, safe_filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    return {"status": "stored", "filename": safe_filename}

@app.get("/previous_imports")
def get_previous_imports():
    """Get list of previously imported PDF files from data/abrechnung/"""
    import hashlib
    from pathlib import Path
    
    abrechnung_dir = Path("data/abrechnung")
    if not abrechnung_dir.exists():
        return {"files": []}
    
    files = []
    for pdf_file in sorted(abrechnung_dir.glob("*.pdf"), reverse=True):
        try:
            # Calculate file hash for tracking
            with open(pdf_file, "rb") as f:
                file_hash = hashlib.md5(f.read()).hexdigest()
            
            files.append({
                "filename": pdf_file.name,
                "filepath": str(pdf_file),
                "file_hash": file_hash,
                "size": pdf_file.stat().st_size,
                "modified": pdf_file.stat().st_mtime
            })
        except Exception as e:
            logger.warning(f"Could not read file {pdf_file}: {e}")
    
    return {"files": files}

@app.post("/reimport_pdf")
def reimport_pdf(filename: str = Form(...), abrechnungsmonat: str = Form(...), import_mode: str = Form("sgbxi")):
    """Re-import a previously imported PDF file"""
    import hashlib
    from pathlib import Path

    abrechnung_dir = Path("data/abrechnung").resolve()
    pdf_path = (abrechnung_dir / os.path.basename(filename)).resolve()

    if abrechnung_dir not in pdf_path.parents or not pdf_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")
    
    try:
        # Calculate file hash
        with open(pdf_path, "rb") as f:
            file_hash = hashlib.md5(f.read()).hexdigest()
        
        # Process the PDF using the standard flow
        logger.info(f"Starting re-import of {filename} (mode: {import_mode}, month: {abrechnungsmonat})")
        
        # Step 1: Prepare chunks (extract, split, cache)
        text = pdf_parser.extract_text_from_pdf(str(pdf_path))
        
        # Extract billing summary from first page
        first_page_text = pdf_parser.extract_first_page_text(str(pdf_path))
        billing_summary = pdf_parser.extract_billing_summary(first_page_text)
        if not billing_summary:
            # If extraction fails, generate random summary data
            billing_summary = pdf_parser.generate_random_billing_summary()
            logger.info(f"Generated random billing summary: {billing_summary}")
        if billing_summary:
            database.insert_billing_summary(billing_summary, abrechnungsmonat)
        
        raw_chunks = pdf_parser.split_into_chunks(text)
        now_ts = datetime.now().isoformat()
        
        chunk_entries = []
        for chunk_text in raw_chunks:
            cid = str(uuid.uuid4())
            chunk_entry = {
                "chunk_id": cid,
                "text": chunk_text,
                "source_pdf": filename,
                "patient_name": None,
                "created_at": now_ts,
            }
            chunk_entries.append(chunk_entry)
        
        pdf_parser.store_chunks_temp(abrechnungsmonat, chunk_entries)
        logger.info(f"Prepared PDF: {len(chunk_entries)} chunks")
        
        # Step 2: Filter and process by mode
        filtered = pdf_parser.filter_chunks_by_mode(chunk_entries, import_mode)
        logger.info(f"Filtered chunks: {len(filtered)} chunks for mode '{import_mode}'")
        
        # Route to appropriate processing function based on mode
        inserted = 0
        if import_mode == "sgbxi":
            inserted = pdf_parser.process_import_sgbxi(filtered, abrechnungsmonat)
        elif import_mode == "sgbv":
            inserted = pdf_parser.process_import_sgbv(filtered, abrechnungsmonat)
        elif import_mode == "verhinderungspflege":
            inserted = pdf_parser.process_import_verhinderungspflege(filtered, abrechnungsmonat)
        elif import_mode == "entleistung":
            # Entleistung also uses process_import_sgbxi (special handling in filter_chunks_by_mode)
            inserted = pdf_parser.process_import_sgbxi(filtered, abrechnungsmonat)
        else:
            raise ValueError(f"Unknown processing mode: {import_mode}")
        
        # Record the import
        database.record_file_import(
            filename=filename,
            abrechnungsmonat=abrechnungsmonat,
            import_mode=import_mode,
            file_hash=file_hash,
            invoice_count=inserted
        )
        
        logger.info(f"✓ Re-import complete: {inserted} invoices processed from {filename}")
        
        return {
            "status": "success",
            "filename": filename,
            "inserted": inserted,
            "mode": import_mode,
            "abrechnungsmonat": abrechnungsmonat
        }
        
    except Exception as e:
        logger.error(f"Error re-importing {filename}: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/generate_invoices")
def generate_invoices(req: InvoiceRequest):
    # First mark invoices ready based on amount_owed and service packet flag
    database.mark_month_ready_for_generation(req.abrechnungsmonat, legacy_entleistung=True)
    # Then generate the invoices
    result = invoice_generator.process_generate_invoices(req.abrechnungsmonat)
    return {
        "status": "ok",
        "message": "Rechnungen erstellt",
        "abrechnungsmonat": req.abrechnungsmonat,
        "generated": result["generated"],
        "total_cases": result["total_cases"],
        "failed": result["failed"],
    }


@app.post("/regenerate_invoices")
def regenerate_invoices(req: InvoiceRequest):
    """
    Regenerate PDFs for an already prepared month without re-running mark_ready.

    This reuses the existing invoice numbers where present and only includes
    billing rows whose linked care_event period actually belongs to the
    requested month.
    """
    result = invoice_generator.process_generate_invoices(
        req.abrechnungsmonat,
        include_orphaned=False,
        require_invoice_needed=False,
    )
    return {
        "status": "ok",
        "message": "Rechnungen neu erstellt",
        "abrechnungsmonat": req.abrechnungsmonat,
        "generated": result["generated"],
        "total_cases": result["total_cases"],
        "failed": result["failed"],
    }


@app.post("/complete_data")
def complete_data(req: InvoiceRequest):
    database.check_missing_patient_fields(req.abrechnungsmonat, auto_fix=True)
    database.check_service_fields(auto_fix=True)
    return {"status": "ok", "message": "Patientendaten ergänzt und Leistungsdaten überprüft"}


@app.post("/check_service_fields")
def check_service_fields():
    database.check_service_fields(auto_fix=False)
    return {"status": "ok", "message": "Leistungsdaten überprüft"}


@app.post("/retry_failed")
def retry_failed():
    pdf_parser.refeed_failed_chunk_from_file()
    return {"status": "ok", "message": "Fehlerhafte Abrechnungen erneut verarbeitet"}


@app.post("/prepare_pdf")
def prepare_pdf(req: ProcessRequest):
    """
    Step 1: User clicks 'Weiter' after upload.
    Extract text → split into chunks → cache for later mode selection.
    """
    chunk_entries = _prepare_chunks_for_file(req.file_name, req.abrechnungsmonat)
    return {
        "status": "prepared",
        "file_name": req.file_name,
        "imported_chunks": len(chunk_entries),
    }





@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    """Quick dashboard shell linking to the RAG workflow."""
    actions = [
        {
            "title": "Chunks & Invoices Browser",
            "path": "/chunks/browse",
            "description": "Review chunk metadata, patient links, and the invoices that were generated from them."
        },
        {
            "title": "Prepare PDF",
            "path": "/prepare_pdf",
            "description": "Upload files through `/upload_pdf` then run `/prepare_pdf` to split and index patient chunks."
        },
        {
            "title": "Process PDF",
            "path": "/process_pdf",
            "description": "Trigger the existing processing flow once chunks are filtered by mode."
        },
        {
            "title": "Ask the Agent",
            "path": "/rag/query",
            "description": "Issue an embedding query and see the structured chunk matches returned as JSON."
        }
    ]
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "actions": actions,
    })


@app.get("/chunks/browse", response_class=HTMLResponse)
def browse_chunks(request: Request, patient_name: str | None = None):
    """Simple UI view to inspect chunk metadata and linked invoices (MongoDB version)."""
    from app.db.mongodb_config import get_database
    
    db = get_database()
    
    # Build query filter
    query = {}
    if patient_name:
        query["patient_name"] = {"$regex": patient_name, "$options": "i"}
    
    # Get chunks from MongoDB
    chunks_cursor = db.chunks.find(query).sort("created_at", -1).limit(300)
    
    chunks = []
    for chunk_doc in chunks_cursor:
        chunk_id = chunk_doc.get("chunk_id") or str(chunk_doc.get("_id"))
        
        # Look up any associated care_events
        care_event = db.care_events.find_one({"origin_chunk_id": chunk_id})
        
        chunks.append({
            "chunk_id": chunk_id,
            "patient_name": chunk_doc.get("patient_name", ""),
            "source_pdf": chunk_doc.get("source_pdf", ""),
            "text_preview": chunk_doc.get("text", "")[:200] if chunk_doc.get("text") else "",
            "created_at": str(chunk_doc.get("created_at", "")),
            "invoice_id": care_event.get("care_event_id") if care_event else None,
            "amount_owed": care_event.get("amount_owed") if care_event else None,
            "invoicing_month": care_event.get("invoicing_month") if care_event else None,
        })

    return templates.TemplateResponse("rag_chunks.html", {
        "request": request,
        "patient_name": patient_name or "",
        "chunks": chunks,
    })


@app.post("/process_pdf")
def process_pdf(req: ProcessRequest):
    """
    Step 2: User selects a mode (sgbxi, sgbv, verhinderungspflege, or entleistung).
    Reuses cached chunks → filters by Pflegekonto → imports to appropriate table.
    """
    chunks = pdf_parser.get_chunks_temp(req.abrechnungsmonat)
    if not chunks:
        # fallback only if cache was lost (e.g. app restarted)
        chunks = _prepare_chunks_for_file(req.file_name, req.abrechnungsmonat)

    filtered = pdf_parser.filter_chunks_by_mode(chunks, req.mode)
    
    # Route to appropriate processing function based on mode
    if req.mode == "sgbxi":
        pdf_parser.process_import_sgbxi(filtered, req.abrechnungsmonat)
    elif req.mode == "sgbv":
        pdf_parser.process_import_sgbv(filtered, req.abrechnungsmonat)
    elif req.mode == "verhinderungspflege":
        pdf_parser.process_import_verhinderungspflege(filtered, req.abrechnungsmonat)
    elif req.mode == "entleistung":
        pdf_parser.process_import_sgbxi(filtered, req.abrechnungsmonat)  # Special 4064 handling in process_import_sgbxi
    else:
        raise ValueError(f"Unknown processing mode: {req.mode}")

    # Auto-validate and fix any reversed sum_covered/sum_total in SGBXI records
    if req.mode in ["sgbxi", "entleistung"]:
        validation_result = database.validate_and_fix_sgbxi_amounts()
        if validation_result['corrected_count'] > 0:
            logger.info(f"Auto-corrected {validation_result['corrected_count']} SGBXI records with reversed amounts")

    return {
        "status": "processed",
        "mode": req.mode,
        "imported_chunks": len(filtered),
    }

LOG_QUEUE: asyncio.Queue[str] = asyncio.Queue()
EVENT_LOOP = None  # set on startup

class TeeStdout:
    """Mirror prints to console and into an asyncio queue for SSE."""
    def __init__(self, real_stdout):
        self.real = real_stdout

    def write(self, data: str):
        # write to real stdout
        self.real.write(data)
        self.real.flush()
        # push each completed line to queue
        if data and data.strip():
            try:
                # schedule thread-safe put to the queue
                if EVENT_LOOP is not None:
                    EVENT_LOOP.call_soon_threadsafe(LOG_QUEUE.put_nowait, data.rstrip())
            except Exception:
                pass

    def flush(self):
        try:
            self.real.flush()
        except Exception:
            pass

@app.get("/logs/stream")
async def logs_stream():
    async def event_generator():
        # small hello so client shows something fast
        yield "event: hello\ndata: log stream connected\n\n"
        while True:
            line = await LOG_QUEUE.get()
            yield f"data: {line}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # for proxies, if any
        },
    )

class MarkReadyRequest(BaseModel):
    invoicing_month: str   # "MMYYYY"
    only_positive: bool = False

@app.post("/mark_ready")
def mark_ready(req: MarkReadyRequest):
    try:
        m = (req.invoicing_month or "").strip()
        if len(m) != 6 or not m.isdigit():
            raise HTTPException(status_code=400, detail="invoicing_month must be MMYYYY")

        updated = database.mark_month_ready_for_generation(m, req.only_positive, legacy_entleistung=False)
        return {"status": "ok", "updated": updated}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/mark_ready error:\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/mark_ready_legacy")
def mark_ready_legacy(req: MarkReadyRequest):
    """
    Mark month ready for generation using LEGACY Entleistung logic.
    Legacy logic: Anything > 125 EUR per month gets invoiced (simple monthly threshold).
    Use this for December 2025 and earlier months before the yearly cumulative logic was implemented.
    """
    try:
        m = (req.invoicing_month or "").strip()
        if len(m) != 6 or not m.isdigit():
            raise HTTPException(status_code=400, detail="invoicing_month must be MMYYYY")

        updated = database.mark_month_ready_for_generation(m, req.only_positive, legacy_entleistung=True)
        return {"status": "ok", "updated": updated}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/mark_ready_legacy error:\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/validate_sgbxi_amounts")
async def validate_sgbxi_amounts():
    """
    Validate all SGBXI records and auto-correct any reversed sum_covered/sum_total.
    
    Rule: sum_covered must always be <= sum_total
    If sum_covered > sum_total, they are automatically swapped.
    
    Returns: counts of corrections made and total records checked.
    """
    try:
        result = database.validate_and_fix_sgbxi_amounts()
        return {
            "status": "success",
            "corrected_count": result['corrected_count'],
            "total_checked": result['total_checked'],
            "message": f"Corrected {result['corrected_count']} out of {result['total_checked']} SGBXI records"
        }
    except Exception as e:
        logger.error(f"/validate_sgbxi_amounts error:\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

# ------------------------------
# Patient CRUD Endpoints
# ------------------------------
class PatientRequest(BaseModel):
    name: str
    birthdate: str
    insurance_number: str
    care_level: str
    address: str = ""
    street_name: str = ""
    street_number: str = ""
    postal_code: str = ""
    city: str = ""
    debtor_number: str = ""
    include_service_packet: bool = False

@app.get("/patients")
def list_patients():
    """List all patients from MongoDB."""
    try:
        from app.db.mongodb_repositories import PatientRepository
        
        patients = PatientRepository.find_all("org_default")
        return {
            "status": "ok",
            "patients": [
                {
                    "id": p.get("patient_id"),
                    "name": p.get("patient_name", ""),
                    "birthdate": p.get("date_of_birth", ""),
                    "insurance_number": p.get("insurance_number", ""),
                    "care_level": p.get("care_level", ""),
                    "address": f"{p.get('street_name', '')} {p.get('street_number', '')}, {p.get('postal_code', '')} {p.get('city', '')}" if p.get("street_name") else "",
                    "street_name": p.get("street_name", ""),
                    "street_number": p.get("street_number", ""),
                    "postal_code": p.get("postal_code", ""),
                    "city": p.get("city", ""),
                    "debtor_number": p.get("debtor_id", "") or "",
                    "include_service_packet": bool(p.get("include_service_packet", False))
                }
                for p in patients
            ]
        }
    except Exception as e:
        logger.error(f"/patients error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/patient")
def create_patient(req: PatientRequest):
    """Create a new patient in MongoDB."""
    try:
        from app.db.mongodb_repositories import PatientRepository
        from app.db.connection import get_database
        from datetime import datetime
        import time
        
        # Generate a numeric patient_id (unique timestamp-based ID)
        # Get the max patient_id from existing patients and increment
        db = get_database()
        org_id = "org_default"
        
        # Find the highest existing patient_id
        max_patient = db.patient_profiles.find_one(
            {"org_id": org_id},
            sort=[("patient_id", -1)]
        )
        
        if max_patient and max_patient.get("patient_id"):
            if isinstance(max_patient["patient_id"], int):
                next_patient_id = max_patient["patient_id"] + 1
            else:
                # Fallback: use timestamp-based ID if existing IDs are not integers
                next_patient_id = int(time.time() * 1000) % (2**31)
        else:
            # If no patients exist, start from a reasonable number
            next_patient_id = 1000
        
        patient_data = {
            "patient_id": next_patient_id,
            "patient_name": req.name,
            "date_of_birth": req.birthdate,
            "insurance_number": req.insurance_number,
            "care_level": req.care_level,
            "street_name": getattr(req, 'street_name', ''),
            "street_number": getattr(req, 'street_number', ''),
            "postal_code": getattr(req, 'postal_code', ''),
            "city": getattr(req, 'city', ''),
            "include_service_packet": bool(req.include_service_packet),
            "debtor_id": ""
        }
        
        PatientRepository.create(patient_data, org_id)
        return {"status": "ok", "patient_id": str(next_patient_id)}
    except Exception as e:
        logger.error(f"/patient POST error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/patient/{patient_id}")
def get_patient(patient_id: str):
    """Get a single patient by ID from MongoDB."""
    try:
        from app.db.mongodb_repositories import PatientRepository
        
        patient = PatientRepository.find_by_id(patient_id, "org_default")
        return {
            "status": "ok",
            "patient": {
                "id": patient.get("patient_id"),
                "name": patient.get("patient_name", ""),
                "birthdate": patient.get("date_of_birth", ""),
                "insurance_number": patient.get("insurance_number", ""),
                "care_level": patient.get("care_level", ""),
                "street_name": patient.get("street_name", ""),
                "street_number": patient.get("street_number", ""),
                "postal_code": patient.get("postal_code", ""),
                "city": patient.get("city", ""),
                "address": f"{patient.get('street_name', '')} {patient.get('street_number', '')}, {patient.get('postal_code', '')} {patient.get('city', '')}" if patient.get("street_name") else "",
                "include_service_packet": bool(patient.get("include_service_packet", False))
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/patient GET error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/patient/{patient_id}")
def update_patient(patient_id: str, req: PatientRequest):
    """Update an existing patient in MongoDB."""
    try:
        from app.db.connection import get_database
        from datetime import datetime
        
        db = get_database()
        org_id = "org_default"
        
        # Try both string and integer formats to handle mixed ID types in DB
        patient_id_int = None
        try:
            patient_id_int = int(patient_id)
        except (ValueError, TypeError):
            pass
        
        # Query with both possible ID formats
        query = {"org_id": org_id}
        if patient_id_int is not None:
            query["patient_id"] = {"$in": [patient_id, patient_id_int]}
        else:
            query["patient_id"] = patient_id
        
        update_data = {
            "patient_name": req.name,
            "date_of_birth": req.birthdate,
            "insurance_number": req.insurance_number,
            "care_level": req.care_level,
            "street_name": getattr(req, 'street_name', ''),
            "street_number": getattr(req, 'street_number', ''),
            "postal_code": getattr(req, 'postal_code', ''),
            "city": getattr(req, 'city', ''),
            "include_service_packet": bool(req.include_service_packet),
            "debtor_id": getattr(req, 'debtor_number', ''),
            "updated_at": datetime.utcnow()
        }
        
        result = db.patient_profiles.update_one(
            query,
            {"$set": update_data}
        )
        
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Patient not found")
        
        return {"status": "ok", "updated": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/patient PUT error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/patient/{patient_id}")
def delete_patient(patient_id: str):
    """Delete a patient and associated care_events from MongoDB."""
    try:
        from app.db.connection import get_database
        
        db = get_database()
        org_id = "org_default"
        
        logger.info(f"Deleting patient {patient_id}")
        
        # Try both string and integer formats to handle mixed ID types in DB
        patient_id_int = None
        try:
            patient_id_int = int(patient_id)
        except (ValueError, TypeError):
            pass
        
        # Query with both possible ID formats
        query = {"org_id": org_id}
        if patient_id_int is not None:
            query["patient_id"] = {"$in": [patient_id, patient_id_int]}
        else:
            query["patient_id"] = patient_id
        
        # Find all care_events for this patient and delete related records
        care_events = list(db.care_events.find(
            query,
            {"_id": 0, "care_event_id": 1}
        ))
        
        logger.info(f"Found {len(care_events)} care_events for patient {patient_id}")
        
        for event in care_events:
            evt_id = event.get("care_event_id")
            if evt_id:
                logger.debug(f"Deleting billing/history for event {evt_id}")
                db.billing_details.delete_many({"org_id": org_id, "care_event_id": evt_id})
                db.care_event_history.delete_many({"org_id": org_id, "care_event_id": evt_id})
        
        # Delete all care_events for this patient (using same query)
        deleted_events = db.care_events.delete_many(query)
        logger.info(f"Deleted {deleted_events.deleted_count} care_events")
        
        # Delete the patient (using same query)
        result = db.patient_profiles.delete_one(query)
        
        if result.deleted_count == 0:
            logger.warning(f"Patient {patient_id} not found")
            raise HTTPException(status_code=404, detail="Patient not found")
        
        logger.info(f"✓ Successfully deleted patient {patient_id}")
        return {"status": "ok", "deleted": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/patient DELETE error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

# ==============================
# Predefined Analytics Dashboard
# ==============================

class PatientHistogramRequest(BaseModel):
    patient_id: int

@app.get("/analytics/patients")
def get_patients():
    """Get list of all patients for dashboard sidebar."""
    try:
        from app.chart_generator import get_all_patients
        
        patients = get_all_patients()
        
        return {
            "status": "ok",
            "patients": patients
        }
    except Exception as e:
        logger.error(f"Patients fetch error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/analytics/patient/{patient_id}/histogram")
def get_patient_histogram(patient_id: str):
    """Generate histogram of invoice amounts by month for a patient."""
    try:
        from app.chart_generator import generate_patient_histogram
        
        chart_data = generate_patient_histogram(patient_id)
        
        return {
            "status": "ok",
            "chart_data": chart_data
        }
    except Exception as e:
        logger.error(f"Histogram generation error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/analytics/billing-summary")
def get_billing_summary():
    """Get billing summary data by care type (SGBXI, Entleistung, SGBV, Verhinderungspflege) grouped by month for stacked bar chart using MongoDB aggregation."""
    try:
        from app.db.connection import get_database
        from datetime import datetime
        
        db = get_database()
        org_id = "org_default"
        
        # Aggregate by both period_start_date and event_type
        pipeline = [
            {"$match": {"org_id": org_id}},
            {
                "$group": {
                    "_id": {
                        "date": "$period_start_date",
                        "event_type": "$event_type"
                    },
                    "total_amount": {"$sum": {"$toDouble": "$sum_total"}},
                    "record_count": {"$sum": 1}
                }
            },
            {"$sort": {"_id.date": 1}}
        ]
        
        results = list(db.care_events.aggregate(pipeline))
        
        # Build monthly data with event types as columns
        month_data = {}
        years_present = set()
        
        for item in results:
            date_str = item.get("_id", {}).get("date", "")
            event_type = item.get("_id", {}).get("event_type", "Unknown")
            total_amount = item.get("total_amount", 0)
            
            if date_str:
                try:
                    # Parse date - handle both DD.MM.YY and DD.MM.YYYY formats
                    date_str = str(date_str).strip()
                    try:
                        dt = datetime.strptime(date_str, "%d.%m.%y")
                    except ValueError:
                        dt = datetime.strptime(date_str, "%d.%m.%Y")
                    
                    month_key = dt.strftime("%m%Y")
                    month_display = dt.strftime("%m/%Y")
                    year = dt.strftime("%Y")
                    years_present.add(year)
                    
                    if month_key not in month_data:
                        month_data[month_key] = {
                            "month": month_display,
                            "year": year,
                            "SGBXI": 0.0,
                            "Entleistung": 0.0,
                            "SGB V": 0.0,
                            "Verhinderungspflege": 0.0,
                            "Beratungsbesuche": 0.0,
                        }
                    
                    # Map event types to columns
                    if event_type == "SGBXI":
                        month_data[month_key]["SGBXI"] += float(total_amount) if total_amount else 0.0
                    elif event_type == "Entleistung":
                        month_data[month_key]["Entleistung"] += float(total_amount) if total_amount else 0.0
                    elif event_type == "SGBV":  # Note: database has SGBV without space
                        month_data[month_key]["SGB V"] += float(total_amount) if total_amount else 0.0
                    elif event_type == "Verhinderungspflege":
                        month_data[month_key]["Verhinderungspflege"] += float(total_amount) if total_amount else 0.0
                    elif event_type == "Consultation":  # Consultation = Beratungsbesuche
                        month_data[month_key]["Beratungsbesuche"] += float(total_amount) if total_amount else 0.0
                        
                except (ValueError, TypeError) as pe:
                    logger.warning(f"Could not parse date: {date_str}, error: {pe}")
        
        # Fill in all 12 months for each year present
        if years_present:
            for year in sorted(years_present):
                for month_num in range(1, 13):
                    month_key = f"{month_num:02d}{year}"
                    if month_key not in month_data:
                        month_display = f"{month_num:02d}/{year}"
                        month_data[month_key] = {
                            "month": month_display,
                            "year": year,
                            "SGBXI": 0.0,
                            "Entleistung": 0.0,
                            "SGB V": 0.0,
                            "Verhinderungspflege": 0.0,
                            "Beratungsbesuche": 0.0,
                        }
        
        # Convert to sorted list, round values
        data = sorted(month_data.values(), key=lambda x: x["month"])
        for row in data:
            row["SGBXI"] = round(row["SGBXI"], 2)
            row["Entleistung"] = round(row["Entleistung"], 2)
            row["SGB V"] = round(row["SGB V"], 2)
            row["Verhinderungspflege"] = round(row["Verhinderungspflege"], 2)
            row["Beratungsbesuche"] = round(row["Beratungsbesuche"], 2)
        
        # Extract unique years for selection
        years = sorted(list(years_present))
        
        return {
            "status": "ok",
            "years": years,
            "data": data
        }
    except Exception as e:
        logger.error(f"Billing summary error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


def _format_date_grouped_analytics(aggregation_results):
    """
    Helper function to format MongoDB aggregation results for analytics endpoints.
    
    Converts aggregation results with _id (date) to monthly data with month display.
    
    Args:
        aggregation_results: List of dicts from MongoDB aggregation with _id, record_count, total_amount
        
    Returns:
        List of dicts with month display and aggregated data
    """
    from datetime import datetime
    
    month_data = {}
    years_present = set()
    
    for result in aggregation_results:
        date_str = result.get("_id", "")
        record_count = result.get("record_count", 0)
        total_amount = result.get("total_amount", 0)
        
        if date_str:
            try:
                # Parse DD.MM.YY format from database
                date_str = date_str.strip()
                dt = datetime.strptime(date_str, "%d.%m.%y")
                month_key = dt.strftime("%m%Y")
                month_display = dt.strftime("%m/%Y")
                year = dt.strftime("%Y")
                years_present.add(year)
                
                if month_key not in month_data:
                    month_data[month_key] = {"month": month_display, "invoice_count": 0, "total_amount": 0.0}
                
                month_data[month_key]["invoice_count"] += record_count
                month_data[month_key]["total_amount"] += float(total_amount) if total_amount else 0.0
            except (ValueError, TypeError) as e:
                logger.warning(f"Could not parse date '{date_str}': {e}")
    
    # Fill in all 12 months for each year present
    if years_present:
        for year in sorted(years_present):
            for month_num in range(1, 13):
                month_key = f"{month_num:02d}{year}"
                if month_key not in month_data:
                    month_display = f"{month_num:02d}/{year}"
                    month_data[month_key] = {"month": month_display, "invoice_count": 0, "total_amount": 0.0}
    
    # Convert to sorted list
    return sorted(month_data.values(), key=lambda x: x["month"])


@app.get("/analytics/sgbv")
def get_sgbv_data():
    """Get SGB V (event_type SGBV) care record data grouped by month using MongoDB aggregation."""
    try:
        from app.db import CareEventRepository
        
        # Use MongoDB aggregation instead of SQL
        results = CareEventRepository.get_summary_by_event_type("SGBV")
        
        # Format results for API response
        data = _format_date_grouped_analytics(results)
        
        return {
            "status": "ok",
            "data": data
        }
    except Exception as e:
        logger.error(f"SGB V data error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/analytics/verhinderungspflege")
def get_verhinderungspflege_data():
    """Get VerhinderungsPflege (event_type Verhinderungspflege) care record data grouped by month using MongoDB aggregation."""
    try:
        from app.db import CareEventRepository
        
        # Use MongoDB aggregation instead of SQL
        results = CareEventRepository.get_summary_by_event_type("Verhinderungspflege")
        
        # Format results for API response
        data = _format_date_grouped_analytics(results)
        
        return {
            "status": "ok",
            "data": data
        }
    except Exception as e:
        logger.error(f"VerhinderungsPflege data error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/analytics/sgbxi")
def get_sgbxi_data():
    """Get SGB XI & Entlastungsleistungen data grouped by month using MongoDB aggregation."""
    try:
        from app.db import CareEventRepository
        
        # Query both SGBXI and Entleistung
        sgbxi_results = CareEventRepository.get_summary_by_event_type("SGBXI")
        entleistung_results = CareEventRepository.get_summary_by_event_type("Entleistung")
        
        # Combine results
        combined_results = sgbxi_results + entleistung_results
        
        # Format results for API response
        data = _format_date_grouped_analytics(combined_results)
        
        return {
            "status": "ok",
            "data": data
        }
    except Exception as e:
        logger.error(f"SGB XI data error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/analytics/sgbv/patient/{patient_id}")
def get_sgbv_by_patient(patient_id: str):
    """Get SGB V care records for a specific patient grouped by month from MongoDB."""
    try:
        from app.db.connection import get_database
        from datetime import datetime
        
        db = get_database()
        org_id = "org_default"
        
        # Try both string and integer formats to handle mixed ID types in DB
        patient_id_int = None
        try:
            patient_id_int = int(patient_id)
        except (ValueError, TypeError):
            pass
        
        # Query with both possible ID formats
        # NOTE: Database stores "SGBV" but we display as "SGB V"
        query = {"org_id": org_id, "event_type": "SGBV"}
        if patient_id_int is not None:
            query["patient_id"] = {"$in": [patient_id, patient_id_int]}
        else:
            query["patient_id"] = patient_id
        
        # Get records grouped by period_start_date
        pipeline = [
            {"$match": query},
            {
                "$group": {
                    "_id": "$period_start_date",
                    "total_amount": {"$sum": {"$toDouble": "$sum_total"}}
                }
            },
            {"$sort": {"_id": 1}}
        ]
        
        rows = list(db.care_events.aggregate(pipeline))
        
        # Build result data, parsing date format and grouping by month
        month_data = {}
        years_present = set()
        
        for row in rows:
            date_str = row.get("_id")
            amount = row.get("total_amount", 0)
            
            if date_str:
                try:
                    # Handle various date formats
                    date_str = str(date_str).strip()
                    # Try DD.MM.YY format first
                    try:
                        dt = datetime.strptime(date_str, "%d.%m.%y")
                    except ValueError:
                        # Try DD.MM.YYYY format
                        dt = datetime.strptime(date_str, "%d.%m.%Y")
                    
                    month_key = dt.strftime("%m%Y")
                    month_display = dt.strftime("%m/%Y")
                    year = dt.strftime("%Y")
                    years_present.add(year)
                    
                    if month_key not in month_data:
                        month_data[month_key] = {"month": month_display, "total_amount": 0.0}
                    
                    month_data[month_key]["total_amount"] += float(amount) if amount else 0.0
                except (ValueError, TypeError) as pe:
                    logger.warning(f"Could not parse date: {date_str}, error: {pe}")
        
        # Fill in all 12 months for each year present
        if years_present:
            for year in sorted(years_present):
                for month_num in range(1, 13):
                    month_key = f"{month_num:02d}{year}"
                    if month_key not in month_data:
                        month_display = f"{month_num:02d}/{year}"
                        month_data[month_key] = {"month": month_display, "total_amount": 0.0}
        
        # Convert to sorted list
        data = sorted(month_data.values(), key=lambda x: x["month"])
        
        return {
            "status": "ok",
            "data": data
        }
    except Exception as e:
        logger.error(f"SGB V patient data error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/analytics/verhinderungspflege/patient/{patient_id}")
def get_verhinderungspflege_by_patient(patient_id: str):
    """Get Verhinderungspflege care records for a specific patient grouped by month from MongoDB."""
    try:
        from app.db.connection import get_database
        from datetime import datetime
        
        db = get_database()
        org_id = "org_default"
        
        # Try both string and integer formats to handle mixed ID types in DB
        patient_id_int = None
        try:
            patient_id_int = int(patient_id)
        except (ValueError, TypeError):
            pass
        
        # Query with both possible ID formats
        query = {"org_id": org_id, "event_type": "Verhinderungspflege"}
        if patient_id_int is not None:
            query["patient_id"] = {"$in": [patient_id, patient_id_int]}
        else:
            query["patient_id"] = patient_id
        
        # Get records grouped by period_start_date
        pipeline = [
            {"$match": query},
            {
                "$group": {
                    "_id": "$period_start_date",
                    "total_amount": {"$sum": {"$toDouble": "$sum_total"}}
                }
            },
            {"$sort": {"_id": 1}}
        ]
        
        rows = list(db.care_events.aggregate(pipeline))
        
        # Build result data, parsing date format and grouping by month
        month_data = {}
        years_present = set()
        
        for row in rows:
            date_str = row.get("_id")
            amount = row.get("total_amount", 0)
            
            if date_str:
                try:
                    # Handle various date formats
                    date_str = str(date_str).strip()
                    # Try DD.MM.YY format first
                    try:
                        dt = datetime.strptime(date_str, "%d.%m.%y")
                    except ValueError:
                        # Try DD.MM.YYYY format
                        dt = datetime.strptime(date_str, "%d.%m.%Y")
                    
                    month_key = dt.strftime("%m%Y")
                    month_display = dt.strftime("%m/%Y")
                    year = dt.strftime("%Y")
                    years_present.add(year)
                    
                    if month_key not in month_data:
                        month_data[month_key] = {"month": month_display, "total_amount": 0.0}
                    
                    month_data[month_key]["total_amount"] += float(amount) if amount else 0.0
                except (ValueError, TypeError) as pe:
                    logger.warning(f"Could not parse date: {date_str}, error: {pe}")
        
        # Fill in all 12 months for each year present
        if years_present:
            for year in sorted(years_present):
                for month_num in range(1, 13):
                    month_key = f"{month_num:02d}{year}"
                    if month_key not in month_data:
                        month_display = f"{month_num:02d}/{year}"
                        month_data[month_key] = {"month": month_display, "total_amount": 0.0}
        
        # Convert to sorted list
        data = sorted(month_data.values(), key=lambda x: x["month"])
        
        return {
            "status": "ok",
            "data": data
        }
    except Exception as e:
        logger.error(f"Verhinderungspflege patient data error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


# NEW UNIFIED SCHEMA ANALYTICS ENDPOINTS
# =====================================

@app.get("/analytics/private-invoices")
def get_private_invoices():
    """Get all billable invoices (SGBXI + Entleistung), excluding Consultations."""
    try:
        from app.db.mongodb_config import get_database
        
        db = get_database()
        
        # MongoDB aggregation pipeline
        pipeline = [
            {
                "$match": {
                    "org_id": "org_default",
                    "event_type": {"$in": ["SGBXI", "Entleistung"]}
                }
            },
            {
                "$lookup": {
                    "from": "patient_profiles",
                    "let": {"patient_id": "$patient_id"},
                    "pipeline": [
                        {"$match": {"$expr": {"$eq": ["$patient_id", "$$patient_id"]}}}
                    ],
                    "as": "patient"
                }
            },
            {"$unwind": {"path": "$patient", "preserveNullAndEmptyArrays": True}},
            {
                "$lookup": {
                    "from": "billing_details",
                    "let": {"care_event_id": "$care_event_id"},
                    "pipeline": [
                        {"$match": {"$expr": {"$eq": ["$care_event_id", "$$care_event_id"]}}}
                    ],
                    "as": "billing"
                }
            },
            {"$unwind": {"path": "$billing", "preserveNullAndEmptyArrays": True}},
            {"$sort": {"created_at": -1}}
        ]
        
        results = list(db.care_events.aggregate(pipeline))
        
        invoices = []
        for doc in results:
            billing = doc.get("billing") or {}
            patient = doc.get("patient") or {}
            invoices.append({
                "care_event_id": doc.get("care_event_id"),
                "patient_name": patient.get("patient_name", ""),
                "period_start_date": doc.get("period_start_date"),
                "period_end_date": doc.get("period_end_date"),
                "billing_status": billing.get("billing_status", "pending"),
                "amount_owed": float(billing.get("amount_owed", 0) or 0),
                "sum_total": float(doc.get("sum_total", 0) or 0),
            })
        
        return {
            "status": "ok",
            "invoices": invoices
        }
    except Exception as e:
        logger.error(f"Private invoices error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/analytics/consultations")
def get_consultations_data():
    """Get Consultations (event_type Consultation) grouped by month using MongoDB aggregation."""
    try:
        from app.db import CareEventRepository
        
        # Use MongoDB aggregation instead of SQL
        results = CareEventRepository.get_summary_by_event_type("Consultation")
        
        # Format results for API response
        data = _format_date_grouped_analytics(results)
        
        return {
            "status": "ok",
            "data": data
        }
    except Exception as e:
        logger.error(f"Consultations data error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/analytics/ausbildungspauschale")
def get_ausbildungspauschale_data():
    """Get Ausbildungspauschale (service_code 01013021) grouped by month."""
    try:
        from app.db.connection import get_database

        db = get_database()
        org_id = "org_default"

        pipeline = [
            {"$match": {"org_id": org_id}},
            {"$unwind": "$services"},
            {"$match": {"services.service_code": "01013021"}},
            {
                "$group": {
                    "_id": "$period_start_date",
                    "record_count": {"$sum": 1},
                    "total_amount": {"$sum": {"$toDouble": "$services.line_total"}}
                }
            },
            {"$sort": {"_id": 1}}
        ]

        rows = list(db.care_events.aggregate(pipeline))
        data = _format_date_grouped_analytics(rows)

        return {
            "status": "ok",
            "data": data
        }
    except Exception as e:
        logger.error(f"Ausbildungspauschale data error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/analytics/consultations/patient/{patient_id}")
def get_consultations_by_patient(patient_id: str):
    """Get Consultations for a specific patient grouped by month from MongoDB."""
    try:
        from app.db.connection import get_database
        from datetime import datetime
        
        db = get_database()
        org_id = "org_default"
        
        # Try both string and integer formats to handle mixed ID types in DB
        patient_id_int = None
        try:
            patient_id_int = int(patient_id)
        except (ValueError, TypeError):
            pass
        
        # Query with both possible ID formats
        query = {"org_id": org_id, "event_type": "Consultation"}
        if patient_id_int is not None:
            query["patient_id"] = {"$in": [patient_id, patient_id_int]}
        else:
            query["patient_id"] = patient_id
        
        # Get records grouped by period_start_date
        pipeline = [
            {"$match": query},
            {
                "$group": {
                    "_id": "$period_start_date",
                    "total_amount": {"$sum": {"$toDouble": "$sum_total"}}
                }
            },
            {"$sort": {"_id": 1}}
        ]
        
        rows = list(db.care_events.aggregate(pipeline))
        
        month_data = {}
        years_present = set()
        
        for row in rows:
            date_str = row.get("_id")
            amount = row.get("total_amount", 0)
            
            if date_str:
                try:
                    # Handle various date formats
                    date_str = str(date_str).strip()
                    # Try DD.MM.YY format first
                    try:
                        dt = datetime.strptime(date_str, "%d.%m.%y")
                    except ValueError:
                        # Try DD.MM.YYYY format
                        dt = datetime.strptime(date_str, "%d.%m.%Y")
                    
                    month_key = dt.strftime("%m%Y")
                    month_display = dt.strftime("%m/%Y")
                    year = dt.strftime("%Y")
                    years_present.add(year)
                    
                    if month_key not in month_data:
                        month_data[month_key] = {"month": month_display, "total_amount": 0.0}
                    
                    month_data[month_key]["total_amount"] += float(amount) if amount else 0.0
                except (ValueError, TypeError) as pe:
                    logger.warning(f"Could not parse date: {date_str}, error: {pe}")
        
        # Fill in all 12 months for each year present
        if years_present:
            for year in sorted(years_present):
                for month_num in range(1, 13):
                    month_key = f"{month_num:02d}{year}"
                    if month_key not in month_data:
                        month_display = f"{month_num:02d}/{year}"
                        month_data[month_key] = {"month": month_display, "total_amount": 0.0}
        
        data = sorted(month_data.values(), key=lambda x: x["month"])
        
        return {
            "status": "ok",
            "data": data
        }
    except Exception as e:
        logger.error(f"Consultations patient data error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/analytics/ausbildungspauschale/patient/{patient_id}")
def get_ausbildungspauschale_by_patient(patient_id: str):
    """Get Ausbildungspauschale (service_code 01013021) for a patient grouped by month."""
    try:
        from app.db.connection import get_database
        from datetime import datetime

        db = get_database()
        org_id = "org_default"

        patient_id_int = None
        try:
            patient_id_int = int(patient_id)
        except (ValueError, TypeError):
            pass

        match_query = {"org_id": org_id, "services.service_code": "01013021"}
        if patient_id_int is not None:
            match_query["patient_id"] = {"$in": [patient_id, patient_id_int]}
        else:
            match_query["patient_id"] = patient_id

        pipeline = [
            {"$match": match_query},
            {"$unwind": "$services"},
            {"$match": {"services.service_code": "01013021"}},
            {
                "$group": {
                    "_id": "$period_start_date",
                    "total_amount": {"$sum": {"$toDouble": "$services.line_total"}}
                }
            },
            {"$sort": {"_id": 1}}
        ]

        rows = list(db.care_events.aggregate(pipeline))

        month_data = {}
        years_present = set()

        for row in rows:
            date_str = row.get("_id")
            amount = row.get("total_amount", 0)

            if date_str:
                try:
                    date_str = str(date_str).strip()
                    try:
                        dt = datetime.strptime(date_str, "%d.%m.%y")
                    except ValueError:
                        dt = datetime.strptime(date_str, "%d.%m.%Y")

                    month_key = dt.strftime("%m%Y")
                    month_display = dt.strftime("%m/%Y")
                    year = dt.strftime("%Y")
                    years_present.add(year)

                    if month_key not in month_data:
                        month_data[month_key] = {"month": month_display, "total_amount": 0.0}

                    month_data[month_key]["total_amount"] += float(amount) if amount else 0.0
                except (ValueError, TypeError) as pe:
                    logger.warning(f"Could not parse date: {date_str}, error: {pe}")

        if years_present:
            for year in sorted(years_present):
                for month_num in range(1, 13):
                    month_key = f"{month_num:02d}{year}"
                    if month_key not in month_data:
                        month_display = f"{month_num:02d}/{year}"
                        month_data[month_key] = {"month": month_display, "total_amount": 0.0}

        data = sorted(month_data.values(), key=lambda x: x["month"])

        return {
            "status": "ok",
            "data": data
        }
    except Exception as e:
        logger.error(f"Ausbildungspauschale patient data error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


# ------------------------------
# Root
# ------------------------------
@app.get("/")
def root():
    return {"message": "Pflegedienst Jung API is running"}


# ------------------------------
# Billing Management (Rechnungsverwaltung)
# ------------------------------

@app.get("/billing/patients")
def get_billing_patients():
    """Get list of patients with billing details."""
    try:
        from app.db.connection import get_database
        db = get_database()
        org_id = "org_default"
        
        # Get all patient_ids with any billing details
        all_care_event_ids = db.billing_details.distinct(
            "care_event_id", 
            {"org_id": org_id}
        )
        
        # Get patient_ids from those care_events
        patient_ids = db.care_events.distinct(
            "patient_id",
            {"org_id": org_id, "care_event_id": {"$in": all_care_event_ids}}
        )
        
        # Get patient info with billing counts
        patients = []
        for pid in patient_ids:
            patient = db.patient_profiles.find_one({"patient_id": pid})
            if patient:
                # Count all billing details for this patient
                patient_care_events = db.care_events.distinct(
                    "care_event_id",
                    {"org_id": org_id, "patient_id": pid}
                )
                billing_count = db.billing_details.count_documents({
                    "org_id": org_id,
                    "care_event_id": {"$in": patient_care_events}
                })
                patients.append({
                    "id": pid,
                    "name": patient.get("patient_name", "Unknown"),
                    "pending_count": billing_count
                })
        
        # Sort by name
        patients.sort(key=lambda x: x["name"])
        
        return {"status": "ok", "patients": patients}
    except Exception as e:
        logger.error(f"Billing patients error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/billing/patient/{patient_id}/pending")
def get_patient_pending_billing(patient_id: str):
    """Get all billing details for a patient."""
    try:
        from app.db.connection import get_database
        db = get_database()
        org_id = "org_default"
        
        # Get care_events for this patient
        care_events = list(db.care_events.find(
            {"org_id": org_id, "patient_id": patient_id}
        ))
        care_event_map = {ce["care_event_id"]: ce for ce in care_events}
        care_event_ids = list(care_event_map.keys())
        
        # Get ALL billing_details for these care_events (no status filter)
        billing_details = list(db.billing_details.find({
            "org_id": org_id,
            "care_event_id": {"$in": care_event_ids}
        }))
        
        # Enrich with care_event info
        result = []
        for bd in billing_details:
            ce = care_event_map.get(bd["care_event_id"], {})
            result.append({
                "billing_detail_id": bd.get("billing_detail_id"),
                "care_event_id": bd.get("care_event_id"),
                "invoicing_month": bd.get("invoicing_month"),
                "sum_covered": bd.get("sum_covered", 0),
                "sum_total": bd.get("sum_total", 0),
                "amount_owed": bd.get("amount_owed", 0),
                "billing_status": bd.get("billing_status"),
                "invoice_number": bd.get("invoice_number"),
                "event_type": ce.get("event_type", ""),
                "period_start_date": ce.get("period_start_date", ""),
                "period_end_date": ce.get("period_end_date", ""),
                "care_account": ce.get("care_account", ""),
                "services_count": len(ce.get("services", []))
            })
        
        # Sort by period_start_date (latest first)
        def parse_german_date(date_str):
            """Parse DD.MM.YY to sortable tuple (year, month, day)"""
            if not date_str:
                return (0, 0, 0)
            try:
                parts = date_str.split(".")
                if len(parts) == 3:
                    day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
                    # Convert 2-digit year to 4-digit
                    year = year + 2000 if year < 50 else year + 1900
                    return (year, month, day)
            except:
                pass
            return (0, 0, 0)
        
        result.sort(key=lambda x: parse_german_date(x.get("period_start_date", "")), reverse=True)
        
        return {"status": "ok", "billing_details": result}
    except Exception as e:
        logger.error(f"Patient pending billing error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/billing/detail/{billing_detail_id}/services")
def get_billing_detail_services(billing_detail_id: str):
    """Get services for a specific billing detail."""
    try:
        from app.db.connection import get_database
        db = get_database()
        org_id = "org_default"
        
        # Get billing_detail
        bd = db.billing_details.find_one({
            "org_id": org_id,
            "billing_detail_id": billing_detail_id
        })
        if not bd:
            raise HTTPException(status_code=404, detail="Billing detail not found")
        
        # Get linked care_event
        ce = db.care_events.find_one({
            "org_id": org_id,
            "care_event_id": bd["care_event_id"]
        })
        if not ce:
            raise HTTPException(status_code=404, detail="Care event not found")
        
        services = ce.get("services", [])
        
        return {
            "status": "ok",
            "billing_detail_id": billing_detail_id,
            "care_event_id": bd["care_event_id"],
            "services": services,
            "sum_covered": bd.get("sum_covered", 0),
            "sum_total": bd.get("sum_total", 0)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get services error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


class ServiceUpdate(BaseModel):
    service_code: str
    service_description: str
    quantity_value: float
    unit_price: float
    line_total: Optional[float] = None


class ServicesUpdateRequest(BaseModel):
    services: List[ServiceUpdate]
    sum_covered: Optional[float] = None


@app.put("/billing/detail/{billing_detail_id}/services")
def update_billing_detail_services(billing_detail_id: str, request: ServicesUpdateRequest):
    """Update services for a billing detail (edit/delete services)."""
    try:
        from app.db.connection import get_database
        db = get_database()
        org_id = "org_default"
        
        # Get billing_detail
        bd = db.billing_details.find_one({
            "org_id": org_id,
            "billing_detail_id": billing_detail_id
        })
        if not bd:
            raise HTTPException(status_code=404, detail="Billing detail not found")

        ce = db.care_events.find_one({
            "org_id": org_id,
            "care_event_id": bd["care_event_id"]
        })
        if not ce:
            raise HTTPException(status_code=404, detail="Care event not found")
        
        # Build updated services with recalculated line_totals
        updated_services = []
        new_sum_total = 0
        for svc in request.services:
            line_total = svc.quantity_value * svc.unit_price
            updated_services.append({
                "service_code": svc.service_code,
                "service_description": svc.service_description,
                "quantity_value": svc.quantity_value,
                "unit_price": svc.unit_price,
                "line_total": round(line_total, 2),
                "updated_at": datetime.now().isoformat()
            })
            new_sum_total += line_total
        
        new_sum_total = round(new_sum_total, 2)
        event_type = ce.get("event_type", "")
        invoicing_month = bd.get("invoicing_month", "")

        entlastung_recompute = None
        if event_type == "Entleistung" and uses_new_entlastung_logic(invoicing_month):
            # Balance-tracked months: covered/owed always come from the balance,
            # never from a manually typed value, so used_amount stays in sync.
            previously_covered = bd.get("sum_covered", 0) or 0
            entlastung_recompute = recompute_entlastung_for_care_event(
                ce.get("patient_id"), invoicing_month, new_sum_total, previously_covered
            )
            new_sum_covered = entlastung_recompute["covered"]
        elif request.sum_covered is not None:
            new_sum_covered = request.sum_covered
        elif event_type == "Entleistung":
            new_sum_covered = min(new_sum_total, 127.35)
        else:
            new_sum_covered = new_sum_total

        investitionskosten = round(new_sum_total * 0.06, 2) if event_type == "SGBXI" else 0.0
        if entlastung_recompute is not None:
            new_amount_owed = entlastung_recompute["owed"]
        elif event_type == "SGBXI":
            new_amount_owed = round(max(new_sum_total - new_sum_covered + investitionskosten, 0), 2)
        else:
            new_amount_owed = round(max(new_sum_total - new_sum_covered, 0), 2)

        # Update care_event services and totals
        db.care_events.update_one(
            {"org_id": org_id, "care_event_id": bd["care_event_id"]},
            {
                "$set": {
                    "services": updated_services,
                    "sum_total": new_sum_total,
                    "sum_covered": new_sum_covered,
                    "updated_at": datetime.now().isoformat()
                }
            }
        )

        # Update billing_detail totals
        db.billing_details.update_one(
            {"org_id": org_id, "billing_detail_id": billing_detail_id},
            {
                "$set": {
                    "sum_total": new_sum_total,
                    "sum_covered": new_sum_covered,
                    "investitionskosten": investitionskosten,
                    "amount_owed": new_amount_owed,
                    "updated_at": datetime.now().isoformat()
                }
            }
        )
        
        return {
            "status": "ok",
            "message": "Services updated successfully",
            "sum_total": new_sum_total,
            "sum_covered": new_sum_covered,
            "amount_owed": new_amount_owed,
            "services_count": len(updated_services)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Update services error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/billing/detail/{billing_detail_id}/regenerate-pdf")
def regenerate_billing_pdf(billing_detail_id: str):
    """Regenerate invoice PDF for a billing detail."""
    try:
        from app.db.connection import get_database
        from app.db.mongodb_repositories import InvoiceRepository
        import subprocess
        
        db = get_database()
        org_id = "org_default"
        
        # Get billing_detail
        bd = db.billing_details.find_one({
            "org_id": org_id,
            "billing_detail_id": billing_detail_id
        })
        if not bd:
            raise HTTPException(status_code=404, detail="Billing detail not found")
        
        # Get linked care_event
        ce = db.care_events.find_one({
            "org_id": org_id,
            "care_event_id": bd["care_event_id"]
        })
        if not ce:
            raise HTTPException(status_code=404, detail="Care event not found")
        
        # Get patient info
        patient_doc = db.patient_profiles.find_one({
            "patient_id": ce["patient_id"]
        })
        if not patient_doc:
            raise HTTPException(status_code=404, detail="Patient not found")
        
        event_type = ce.get("event_type", "")
        amounts = database._resolve_invoice_amounts(event_type, ce, bd)
        
        # Assign invoice number if not present
        invoice_number = bd.get("invoice_number")
        if not invoice_number:
            invoice_number = InvoiceRepository.get_next_invoice_number()
            # Update billing_detail with invoice number
            db.billing_details.update_one(
                {"org_id": org_id, "billing_detail_id": billing_detail_id},
                {"$set": {"invoice_number": invoice_number, "updated_at": datetime.now().isoformat()}}
            )
        
        # Build address from split fields
        street_name = patient_doc.get("street_name", "") or ""
        street_number = patient_doc.get("street_number", "") or ""
        postal_code = patient_doc.get("postal_code", "") or ""
        city = patient_doc.get("city", "") or ""
        address = f"{street_name} {street_number} {postal_code} {city}".strip()
        
        # Map services from MongoDB format to template format
        mongo_services = ce.get("services", [])
        mapped_services = [
            {
                "id": idx,
                "code": svc.get("service_code", ""),
                "description": svc.get("service_description", ""),
                "quantity": svc.get("quantity_value", 0),
                "unit_price": svc.get("unit_price", 0),
                "total_price": svc.get("line_total", 0)
            }
            for idx, svc in enumerate(mongo_services)
        ]
        
        # Build data dict matching what generate_invoice_pdf expects
        data = {
            "patient": {
                "id": patient_doc.get("patient_id"),
                "name": patient_doc.get("patient_name", ""),
                "birthdate": patient_doc.get("date_of_birth", ""),
                "insurance_number": patient_doc.get("insurance_number", ""),
                "care_level": patient_doc.get("care_level", ""),
                "include_service_packet": patient_doc.get("include_service_packet", 0) if event_type == "SGBXI" else 0,
                "address": address,
                "debtor_number": patient_doc.get("debtor_id", "") or patient_doc.get("debtor_number", ""),
            },
            "invoice": {
                "id": ce.get("care_event_id"),
                "invoicing_month": bd.get("invoicing_month", ""),
                "invoice_number": invoice_number,
                "care_account": ce.get("care_account", ""),
                "event_type": event_type,
                "care_range_begin": ce.get("period_start_date", ""),
                "care_range_end": ce.get("period_end_date", ""),
                "sum_total": amounts["sum_total"],
                "sum_covered": amounts["sum_covered"],
                "amount_owed": amounts["amount_owed"],
            },
            "services": mapped_services
        }
        
        # Import invoice generator
        from app.invoice_generator import generate_invoice_pdf
        
        # Generate PDF
        pdf_path = generate_invoice_pdf(data)
        
        return {
            "status": "ok",
            "message": "PDF generated successfully",
            "pdf_path": str(pdf_path)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Regenerate PDF error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/billing/detail/{billing_detail_id}/view-pdf")
def view_billing_pdf(billing_detail_id: str):
    """Open the generated PDF for a billing detail."""
    try:
        from app.db.connection import get_database
        import subprocess
        from pathlib import Path
        
        db = get_database()
        org_id = "org_default"
        
        # Get billing_detail
        bd = db.billing_details.find_one({
            "org_id": org_id,
            "billing_detail_id": billing_detail_id
        })
        if not bd:
            raise HTTPException(status_code=404, detail="Billing detail not found")
        
        invoice_number = bd.get("invoice_number")
        if not invoice_number:
            raise HTTPException(status_code=400, detail="Keine Rechnung generiert. Bitte zuerst PDF generieren.")
        
        # Find PDF file by invoice number
        output_dir = Path("output/invoices")
        pdf_files = list(output_dir.glob(f"*{invoice_number}*.pdf"))
        
        if not pdf_files:
            raise HTTPException(status_code=404, detail=f"PDF nicht gefunden für Rechnungsnummer {invoice_number}")
        
        # Open the most recent matching PDF
        pdf_path = max(pdf_files, key=lambda p: p.stat().st_mtime)
        subprocess.run(["open", str(pdf_path)], check=False)
        
        return {
            "status": "ok",
            "message": "PDF opened",
            "pdf_path": str(pdf_path)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"View PDF error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


class BillingStatusUpdate(BaseModel):
    billing_status: str


@app.patch("/billing/detail/{billing_detail_id}/status")
def update_billing_status(billing_detail_id: str, request: BillingStatusUpdate):
    """Update the billing status of a billing detail."""
    valid_statuses = ["", "invoice_needed", "sent", "paid"]
    if request.billing_status not in valid_statuses:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid status. Must be one of: {valid_statuses}"
        )
    
    try:
        from app.db.connection import get_database
        db = get_database()
        
        org_id = "org_default"
        result = db.billing_details.update_one(
            {"org_id": org_id, "billing_detail_id": billing_detail_id},
            {"$set": {"billing_status": request.billing_status}}
        )
        
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Billing detail not found")
        
        return {"status": "ok", "billing_status": request.billing_status}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Update billing status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
