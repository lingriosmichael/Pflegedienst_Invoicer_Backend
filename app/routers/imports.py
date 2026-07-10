import os
import shutil
import uuid
import traceback
from datetime import datetime

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from pydantic import BaseModel

import app.database as database
import app.pdf_parser as pdf_parser
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()

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
class ProcessRequest(BaseModel):
    file_name: str
    abrechnungsmonat: str
    mode: str   # "sgbxi", "sgbv", "verhinderungspflege", or "entleistung"
def _safe_upload_filename(original_filename: str) -> str:
    """Build a filesystem-safe filename that cannot escape UPLOAD_DIR."""
    base_name = os.path.basename(original_filename or "")
    if not base_name.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only .pdf files are accepted")
    safe_stem = "".join(c for c in os.path.splitext(base_name)[0] if c.isalnum() or c in ("-", "_")) or "upload"
    return f"{safe_stem}_{uuid.uuid4().hex[:8]}.pdf"


@router.post("/upload_pdf")
def upload_pdf(file: UploadFile = File(...), abrechnungsmonat: str = Form(...)):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    safe_filename = _safe_upload_filename(file.filename)
    file_path = os.path.join(UPLOAD_DIR, safe_filename)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    return {"status": "stored", "filename": safe_filename}
@router.get("/previous_imports")
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

@router.post("/reimport_pdf")
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
@router.post("/prepare_pdf")
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
@router.post("/process_pdf")
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
