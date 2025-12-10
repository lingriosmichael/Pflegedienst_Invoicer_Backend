from fastapi import FastAPI, UploadFile, File, Form, Request
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi import HTTPException
from app.ai_schema import router as ai_schema_router
from app.openai_client import ( 
                               generate_sql_from_question, 
                               execute_sql, 
                               generate_visualization
                               )
import traceback
import shutil
import os
import asyncio
import app.database as database
import app.invoice_generator as invoice_generator
import app.pdf_parser as pdf_parser
from app.db.migrations import migrate_care_records_to_services_table
import uuid
from datetime import datetime
import logging
from app.core.logging import setup_logging, get_logger

logger = get_logger(__name__)
setup_logging()

app = FastAPI()
app.include_router(ai_schema_router)
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

class RegenerateRequest(BaseModel):
    invoice_num: str


# ------------------------------
# Startup
# ------------------------------
@app.on_event("startup")
async def startup_event():
    try:
        database.init_db()
        # Run migration for existing databases
        migrate_care_records_to_services_table()
    except Exception as e:
        logger.error(f"Failed to init database: {e}")


# ------------------------------
# Endpoints
# ------------------------------
@app.post("/upload_pdf")
def upload_pdf(file: UploadFile = File(...), abrechnungsmonat: str = Form(...)):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    file_path = os.path.join(UPLOAD_DIR, file.filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    return {"status": "stored", "filename": file.filename}

@app.post("/generate_invoices")
def generate_invoices(req: InvoiceRequest):
    # First mark invoices ready based on amount_owed and service packet flag
    database.mark_month_ready_for_generation(req.abrechnungsmonat)
    # Then generate the invoices
    invoice_generator.process_generate_invoices(req.abrechnungsmonat)
    return {"status": "ok", "message": "Rechnungen erstellt"}


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


@app.post("/regenerate_invoice")
def regenerate_invoice(req: RegenerateRequest):
    invoice_generator.regenerate_invoice(req.invoice_num)
    return {"status": "ok", "message": f"Rechnung {req.invoice_num} wurde neu erstellt"}

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
    """Simple UI view to inspect chunk metadata and linked invoices."""
    from app.db.connection import get_db

    base_query = """
    SELECT
        c.id, c.patient_name, c.source_pdf, c.text_preview, c.created_at,
        i.id, i.amount_owed, i.invoicing_month
    FROM chunks c
    LEFT JOIN invoices i ON i.origin_chunk_id = c.id
    """
    params: list[str] = []
    filters = []
    if patient_name:
        filters.append("c.patient_name LIKE ?")
        params.append(f"%{patient_name}%")

    if filters:
        base_query += " WHERE " + " AND ".join(filters)

    base_query += " ORDER BY c.created_at DESC LIMIT 300"

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(base_query, tuple(params))
        rows = cursor.fetchall()

    chunks = []
    for row in rows:
        chunks.append({
            "chunk_id": row[0],
            "patient_name": row[1] or "",
            "source_pdf": row[2],
            "text_preview": row[3],
            "created_at": row[4],
            "invoice_id": row[5],
            "amount_owed": row[6],
            "invoicing_month": row[7],
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

        updated = database.mark_month_ready_for_generation(m, req.only_positive)
        return {"status": "ok", "updated": updated}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/mark_ready error:\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/ai/visualize")
async def ai_visualize(request: Request):
    """
    Generates both SQL and visualization (chart or table)
    based on a natural-language question.
    """
    body = await request.json()
    question = body.get("question")
    mode = body.get("mode", "auto")

    if not question:
        raise HTTPException(status_code=400, detail="Missing 'question'")

    try:
        result = generate_visualization(question, mode)
        return result
    except Exception as e:
        logger.error(f"AI visualization error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ------------------------------
# Patient CRUD Endpoints
# ------------------------------
class PatientRequest(BaseModel):
    name: str
    birthdate: str
    insurance_number: str
    care_level: str
    address: str
    debtor_number: str
    include_service_packet: bool = False

@app.get("/patients")
def list_patients():
    """List all patients."""
    try:
        from app.db.connection import get_db
        with get_db() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT id, name, birthdate, insurance_number, care_level, address, debtor_number, include_service_packet
                FROM patients
                ORDER BY name
            """)
            rows = c.fetchall()
            return {
                "status": "ok",
                "patients": [
                    {
                        "id": r[0],
                        "name": r[1],
                        "birthdate": r[2],
                        "insurance_number": r[3],
                        "care_level": r[4],
                        "address": r[5],
                        "debtor_number": r[6],
                        "include_service_packet": bool(r[7])
                    }
                    for r in rows
                ]
            }
    except Exception as e:
        logger.error(f"/patients error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/patient")
def create_patient(req: PatientRequest):
    """Create a new patient."""
    try:
        from app.db.connection import get_db
        with get_db() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO patients (name, birthdate, insurance_number, care_level, address, debtor_number, include_service_packet)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (req.name, req.birthdate, req.insurance_number, req.care_level, req.address, req.debtor_number, int(req.include_service_packet)))
            conn.commit()
            patient_id = c.lastrowid
            return {"status": "ok", "patient_id": patient_id}
    except Exception as e:
        logger.error(f"/patient POST error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/patient/{patient_id}")
def get_patient(patient_id: int):
    """Get a single patient by ID."""
    try:
        from app.db.connection import get_db
        with get_db() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT id, name, birthdate, insurance_number, care_level, address, debtor_number, include_service_packet
                FROM patients WHERE id = ?
            """, (patient_id,))
            row = c.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Patient not found")
            return {
                "status": "ok",
                "patient": {
                    "id": row[0],
                    "name": row[1],
                    "birthdate": row[2],
                    "insurance_number": row[3],
                    "care_level": row[4],
                    "address": row[5],
                    "debtor_number": row[6],
                    "include_service_packet": bool(row[7])
                }
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/patient GET error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/patient/{patient_id}")
def update_patient(patient_id: int, req: PatientRequest):
    """Update an existing patient."""
    try:
        from app.db.connection import get_db
        with get_db() as conn:
            c = conn.cursor()
            c.execute("""
                UPDATE patients
                SET name = ?, birthdate = ?, insurance_number = ?, care_level = ?, address = ?, debtor_number = ?, include_service_packet = ?
                WHERE id = ?
            """, (req.name, req.birthdate, req.insurance_number, req.care_level, req.address, req.debtor_number, int(req.include_service_packet), patient_id))
            conn.commit()
            if c.rowcount == 0:
                raise HTTPException(status_code=404, detail="Patient not found")
            return {"status": "ok", "updated": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/patient PUT error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/patient/{patient_id}")
def delete_patient(patient_id: int):
    """Delete a patient (and associated invoices/services cascade)."""
    try:
        from app.db.connection import get_db
        with get_db() as conn:
            c = conn.cursor()
            # Delete invoices and services for this patient (cascade)
            c.execute("SELECT id FROM invoices WHERE patient_id = ?", (patient_id,))
            invoice_ids = [row[0] for row in c.fetchall()]
            for inv_id in invoice_ids:
                c.execute("DELETE FROM services WHERE invoice_id = ?", (inv_id,))
            c.execute("DELETE FROM invoices WHERE patient_id = ?", (patient_id,))
            c.execute("DELETE FROM patients WHERE id = ?", (patient_id,))
            conn.commit()
            if c.rowcount == 0:
                raise HTTPException(status_code=404, detail="Patient not found")
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
def get_patient_histogram(patient_id: int):
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
        
        return {
            "status": "ok",
            "message": "Patients and histogram data retrieved"
        }


# ------------------------------
# Root
# ------------------------------
@app.get("/")
def root():
    return {"message": "Pflegedienst Jung API is running"}

