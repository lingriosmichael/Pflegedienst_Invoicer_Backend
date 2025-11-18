import os
import argparse
import uuid
import logging
from datetime import datetime

from app.pdf_parser import extract_text_from_pdf, split_into_chunks
from app.database import insert_chunk
from app.rag_store import RagStore

# Optional: use background enqueue if running inside the app process
try:
    from app.background import enqueue_index_chunks
    _HAS_BG = True
except Exception:
    _HAS_BG = False

logger = logging.getLogger("backfill_rag")
logging.basicConfig(level=logging.INFO)

DATA_DIR = os.getenv("ABRECHNUNG_DIR", "data/abrechnung")


def process_pdf_file(path: str, dry_run: bool = True, use_worker: bool = False, limit_chunks: int | None = None):
    logger.info(f"Processing PDF: {path}")
    text = extract_text_from_pdf(path)
    chunks = split_into_chunks(text)
    logger.info(f"Found {len(chunks)} chunks in {os.path.basename(path)}")

    to_index = []
    now_ts = datetime.now().isoformat()

    for i, ch in enumerate(chunks):
        if limit_chunks is not None and i >= limit_chunks:
            break
        cid = str(uuid.uuid4())
        preview = (ch or "")[:300]

        logger.info(f"Chunk {i+1}: id={cid} preview={preview[:80]!r}")

        if not dry_run:
            try:
                insert_chunk(cid, None, os.path.basename(path), preview, now_ts)
            except Exception as e:
                logger.error(f"DB insert_chunk failed for {cid}: {e}")

        to_index.append({
            "chunk_id": cid,
            "text": ch,
            "source_pdf": os.path.basename(path),
            "patient_name": None,
            "created_at": now_ts,
        })

    # Indexing
    if dry_run:
        logger.info(f"Dry-run: would index {len(to_index)} chunks for {os.path.basename(path)}")
        return len(to_index)

    if use_worker and _HAS_BG:
        logger.info("Enqueuing chunks to background worker for indexing")
        enqueue_index_chunks(to_index)
        return len(to_index)

    # Direct indexing path
    rag = RagStore()
    indexed = 0
    for ch in to_index:
        try:
            rag.add_chunk(ch["chunk_id"], ch["text"], {"patient_name": ch.get("patient_name") or "", "source_pdf": ch.get("source_pdf")})
            indexed += 1
        except Exception as e:
            logger.error(f"Failed to index chunk {ch['chunk_id']}: {e}")
    logger.info(f"Indexed {indexed}/{len(to_index)} chunks for {os.path.basename(path)}")
    return indexed


def main():
    p = argparse.ArgumentParser(description="Backfill existing PDFs into Chroma RAG index")
    p.add_argument("--dir", default=DATA_DIR, help="Directory with PDFs (default: data/abrechnung)")
    p.add_argument("--dry-run", action="store_true", help="Don't write DB or call embeddings; just report")
    p.add_argument("--use-worker", action="store_true", help="Enqueue tasks to background worker if available")
    p.add_argument("--limit-files", type=int, default=None, help="Limit number of files to process")
    p.add_argument("--limit-chunks", type=int, default=None, help="Limit number of chunks per file")
    args = p.parse_args()

    files = [
        os.path.join(args.dir, f)
        for f in os.listdir(args.dir)
        if f.lower().endswith(".pdf")
    ]
    files.sort()

    if args.limit_files:
        files = files[: args.limit_files]

    logger.info(f"Found {len(files)} pdf files in {args.dir}")

    total_indexed = 0
    results_by_file = {}
    failed_files = []

    for i, f in enumerate(files, start=1):
        try:
            logger.info(f"[{i}/{len(files)}] Processing {os.path.basename(f)}...")
            n = process_pdf_file(f, dry_run=args.dry_run, use_worker=args.use_worker, limit_chunks=args.limit_chunks)
            total_indexed += n
            results_by_file[os.path.basename(f)] = {"status": "ok", "chunks": n}
        except Exception as e:
            logger.exception(f"Failed to process {f}: {e}")
            failed_files.append((os.path.basename(f), str(e)))
            results_by_file[os.path.basename(f)] = {"status": "failed", "error": str(e)}

    # Print summary
    print("\n" + "="*80)
    print("BACKFILL SUMMARY")
    print("="*80)
    print(f"Total files: {len(files)}")
    print(f"Successful: {len(files) - len(failed_files)}")
    print(f"Failed: {len(failed_files)}")
    print(f"Total chunks processed: {total_indexed}")
    print(f"Mode: {'dry-run' if args.dry_run else 'live'} | Worker: {args.use_worker}")
    if failed_files:
        print("\nFailed files:")
        for fname, err in failed_files:
            print(f"  - {fname}: {err}")
    print("="*80 + "\n")


if __name__ == "__main__":
    main()
