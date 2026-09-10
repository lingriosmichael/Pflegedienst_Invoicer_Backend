import hashlib
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from pydantic import BaseModel

from app import pdf_parser
from app import database as accounting_database
from app.db import DEFAULT_ORG_ID
from app.db.mongodb_config import get_database
from app.db.transactions import transactional
from app.utils.validation import BillingMonth, validate_month


router = APIRouter()
UPLOAD_DIR = Path("data/abrechnung")
ImportMode = Literal["sgbxi", "sgbv", "verhinderungspflege", "entleistung"]
ImportEngine = Literal["llm", "deterministic"]
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class ProcessRequest(BaseModel):
    file_name: str
    abrechnungsmonat: BillingMonth
    mode: ImportMode = "sgbxi"
    import_id: str | None = None
    engine: ImportEngine = "llm"


class RetryRequest(BaseModel):
    import_id: str
    mode: ImportMode
    engine: ImportEngine = "llm"


def resolve_pdf(filename):
    root = Path(UPLOAD_DIR).resolve()
    candidate = root / filename
    if candidate.is_symlink():
        raise HTTPException(status_code=404, detail="Uploaded PDF not found")
    path = candidate.resolve()
    if path.parent != root or path.suffix.lower() != ".pdf" or not path.is_file():
        raise HTTPException(status_code=404, detail="Uploaded PDF not found")
    return path


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_upload_filename(original_filename):
    name = Path(original_filename or "").name
    if Path(name).suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")
    stem = re.sub(r"[^a-zA-Z0-9_-]", "", Path(name).stem)[:100] or "upload"
    return f"{stem}_{uuid.uuid4().hex}.pdf"


@router.post("/upload_pdf")
def upload_pdf(file: UploadFile = File(...), abrechnungsmonat: str | None = Form(None)):
    if abrechnungsmonat:
        validate_month(abrechnungsmonat)
    root = Path(UPLOAD_DIR)
    root.mkdir(parents=True, exist_ok=True)
    filename = _safe_upload_filename(file.filename)
    path = root / filename
    size = 0
    try:
        with path.open("xb") as stream:
            path.chmod(0o600)
            while block := file.file.read(1024 * 1024):
                size += len(block)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="PDF exceeds 50 MB")
                stream.write(block)
        with path.open("rb") as stream:
            if b"%PDF-" not in stream.read(1024):
                raise HTTPException(status_code=400, detail="Invalid PDF")
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return {"status": "stored", "filename": filename}


@router.get("/previous_imports")
def get_previous_imports():
    files = []
    for path in sorted(Path(UPLOAD_DIR).glob("*.pdf"), reverse=True):
        safe_path = resolve_pdf(path.name)
        stat = safe_path.stat()
        files.append({"filename": path.name, "file_hash": file_hash(safe_path),
                      "size": stat.st_size, "modified": stat.st_mtime})
    return {"files": files}


@transactional
def register_import(document, summary):
    database = get_database()
    identity = {"org_id": DEFAULT_ORG_ID, "import_id": document["import_id"]}
    if database.import_jobs.find_one(identity):
        return
    database.import_jobs.insert_one(document)
    if summary:
        database.billing_summary.update_one(
            {"org_id": DEFAULT_ORG_ID, "abrechnungsmonat": document["invoicing_month"]},
            {"$inc": summary, "$set": {"updated_at": datetime.now(timezone.utc)}}, upsert=True)


def prepare_import(filename, month):
    validate_month(month)
    if get_database().care_events.find_one({"org_id": DEFAULT_ORG_ID, "invoicing_month": month,
            "event_type": {"$ne": "ServicePacket"}, "import_identity_version": {"$ne": 1}}):
        raise HTTPException(409, "This month contains legacy imports; reconcile source identities before importing again")
    path = resolve_pdf(filename)
    digest = file_hash(path)
    import_id = hashlib.sha256(f"{DEFAULT_ORG_ID}:{month}:{digest}".encode()).hexdigest()
    chunks = pdf_parser.split_into_chunks(pdf_parser.extract_text_from_pdf(str(path)))
    summary = pdf_parser.extract_billing_summary(pdf_parser.extract_first_page_text(str(path)))
    document = {"org_id": DEFAULT_ORG_ID, "import_id": import_id, "invoicing_month": month,
                "file_name": path.name, "file_hash": digest, "prepared_chunks": len(chunks),
                "summary_available": bool(summary), "created_at": datetime.now(timezone.utc)}
    register_import(document, summary)
    return get_database().import_jobs.find_one({"org_id": DEFAULT_ORG_ID, "import_id": import_id})


def load_chunks(job):
    path = resolve_pdf(job["file_name"])
    if file_hash(path) != job["file_hash"]:
        raise HTTPException(status_code=409, detail="Uploaded PDF changed; prepare a new import")
    texts = pdf_parser.split_into_chunks(pdf_parser.extract_text_from_pdf(str(path)))
    return [{"text": text, "source_pdf": job["file_name"],
             "chunk_id": hashlib.sha256(f'{job["file_hash"]}:{index}:{text}'.encode()).hexdigest()}
            for index, text in enumerate(texts)]


@router.post("/prepare_pdf")
def prepare_pdf(req: ProcessRequest):
    job = prepare_import(req.file_name, req.abrechnungsmonat)
    return {"status": "prepared", "import_id": job["import_id"], "file_name": job["file_name"],
            "prepared_chunks": job["prepared_chunks"], "imported_chunks": job["prepared_chunks"],
            "summary_available": job["summary_available"]}


def process_job(job, mode, failed_only=False, engine="llm"):
    chunks = pdf_parser.filter_chunks_by_mode(load_chunks(job), mode)
    if failed_only:
        failed_ids = job.get("results", {}).get(mode, {}).get("failed_ids", [])
        chunks = [chunk for chunk in chunks if chunk["chunk_id"] in failed_ids]
    helper = {"sgbxi": pdf_parser.process_import_sgbxi, "entleistung": pdf_parser.process_import_sgbxi,
              "sgbv": pdf_parser.process_import_sgbv,
              "verhinderungspflege": pdf_parser.process_import_verhinderungspflege}[mode]
    result = helper(chunks, job["invoicing_month"], engine=engine)
    rematch = None
    if mode in {"sgbv", "verhinderungspflege"} and result["committed"]:
        accounting_database.mark_month_ready_for_generation(
            job["invoicing_month"],
            event_types=["SGBV" if mode == "sgbv" else "Verhinderungspflege"],
        )
    if mode == "entleistung" and result["committed"]:
        # Entlastungsleistung is assumed fully covered on import. This creates
        # its durable audit marker now; only a later confirmed RZH correction
        # may turn that individual row into invoice_needed.
        accounting_database.mark_month_ready_for_generation(
            job["invoicing_month"],
            event_types=["Entleistung"],
        )
        accounting_database.expire_stale_entlastung_coverage()
        try:
            from app.rzh_reconciliation import retry_unresolved_matches
            rematch = retry_unresolved_matches()
        except Exception as error:
            logger.warning("Post-import RZH rematch failed (%s)", type(error).__name__)
            rematch = {"status": "failed", "financial_effects_applied": False}
    get_database().import_jobs.update_one(
        {"org_id": DEFAULT_ORG_ID, "import_id": job["import_id"]},
        {"$set": {f"results.{mode}": result, "updated_at": datetime.now(timezone.utc)}})
    return {**result, "status": "partial" if result["failed"] else "processed",
            "mode": mode, "import_id": job["import_id"], "imported_chunks": result["committed"],
            "inserted": result["committed"], "reconciliation_rematch": rematch}


@router.post("/process_pdf")
def process_pdf(req: ProcessRequest):
    if req.import_id:
        job = get_database().import_jobs.find_one({"org_id": DEFAULT_ORG_ID, "import_id": req.import_id})
        if not job or job["invoicing_month"] != req.abrechnungsmonat or job["file_hash"] != file_hash(resolve_pdf(req.file_name)):
            raise HTTPException(status_code=409, detail="Import identity does not match the file and month")
    else:
        job = prepare_import(req.file_name, req.abrechnungsmonat)
    return process_job(job, req.mode, engine=req.engine)


@router.post("/reimport_pdf")
def reimport_pdf(filename: str = Form(...), abrechnungsmonat: BillingMonth = Form(...),
                 import_mode: ImportMode = Form("sgbxi"), engine: ImportEngine = Form("llm")):
    job = prepare_import(filename, abrechnungsmonat)
    return process_job(job, import_mode, engine=engine)


@router.post("/retry_failed")
def retry_failed(req: RetryRequest):
    job = get_database().import_jobs.find_one({"org_id": DEFAULT_ORG_ID, "import_id": req.import_id})
    if not job:
        raise HTTPException(status_code=404, detail="Import not found")
    return process_job(job, req.mode, failed_only=True, engine=req.engine)
