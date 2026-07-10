import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()

templates = Jinja2Templates(directory="templates")

@router.get("/dashboard", response_class=HTMLResponse)
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


@router.get("/chunks/browse", response_class=HTMLResponse)
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

@router.get("/logs/stream")
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
@router.get("/")
def root():
    return {"message": "Pflegedienst Jung API is running"}
