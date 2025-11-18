import threading
import queue
import time
import logging
from typing import Any, Dict, List

from app.rag_store import RagStore
from app.database import insert_chunk

logger = logging.getLogger(__name__)

# Simple background queue for indexing tasks
_INDEX_QUEUE: "queue.Queue[Dict[str, Any]]" = queue.Queue()
_WORKER_THREAD: threading.Thread | None = None
_RUNNING = False


def start_worker():
    global _WORKER_THREAD, _RUNNING
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return
    _RUNNING = True
    _WORKER_THREAD = threading.Thread(target=_worker_loop, daemon=True, name="RagIndexWorker")
    _WORKER_THREAD.start()
    logger.info("RAG index worker started")


def stop_worker():
    global _RUNNER
    # Not strictly necessary in this simple impl
    pass


def enqueue_index_chunks(chunks: List[Dict[str, Any]]):
    """Enqueue a list of chunks to be indexed in background.

    Each chunk dict should contain: chunk_id, text, source_pdf, patient_name, created_at
    """
    _INDEX_QUEUE.put({"chunks": chunks})
    logger.debug(f"Enqueued {len(chunks)} chunks for indexing")


def _worker_loop():
    rag = None
    try:
        rag = RagStore()
    except Exception as e:
        logger.error(f"Failed to init RagStore for background worker: {e}")
        return

    while True:
        try:
            task = _INDEX_QUEUE.get()
            if not task:
                time.sleep(0.5)
                continue
            chunks = task.get("chunks", [])
            for ch in chunks:
                try:
                    chunk_id = ch.get("chunk_id")
                    text = ch.get("text")
                    source_pdf = ch.get("source_pdf")
                    patient_name = ch.get("patient_name")
                    created_at = ch.get("created_at")

                    # persist chunk metadata to SQLite (safe even if already present)
                    insert_chunk(chunk_id, patient_name, source_pdf, (text or "")[:300], created_at)

                    # add to chroma
                    rag.add_chunk(chunk_id, text or "", {"patient_name": patient_name or "", "source_pdf": source_pdf or ""})
                    logger.info(f"Indexed chunk {chunk_id}")
                except Exception as e:
                    logger.error(f"Failed to process chunk in background: {e}")
        except Exception as e:
            logger.error(f"Index worker loop error: {e}")
            time.sleep(1)