"""Independent, deterministic PDF audit for SGB XI Abrechnung PDFs.

Extracts per-line service data (code, description, quantity, unit price,
line total) directly from the RZH Abrechnung PDFs using fixed text-layout
parsing (no OpenAI call, no dependency on the care_events import pipeline).
Persists into pdf_audit_documents / pdf_audit_lines so the dashboard's
stored-data totals can be diffed against what the source PDFs actually say,
instead of only reflecting whatever the import pipeline previously wrote to
care_events.

This module never reads or writes care_events, billing_details, or any other
collection used by invoicing/entitlement — it is a read-only check against
the filesystem PDFs, additive to a pair of dedicated audit collections.
"""

import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import fitz

from app.db import DEFAULT_ORG_ID
from app.db.mongodb_config import get_database
from app.utils.parsing import GermanDecimalParser, generate_id

logger = logging.getLogger(__name__)

_SERVICE_LINE = re.compile(r"^(\d+)\s+(\S{6,10})\s+(.+)$")
_AMOUNT_LINE = re.compile(r"^-?[\d.]+,\d{2}$")
_EINREICHUNG = re.compile(r"Einreichung vom (\d{2})\.(\d{2})\.(\d{4})")
_BELEGE_SUMMARY = re.compile(r"Wert\s+eingereichter\s+Belege\s+(\d+)\s+([\d.,]+)")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _detected_billing_month(first_page_text: str) -> str | None:
    """RZH submits ~1 month after the service month: 'Einreichung vom 10.02.2025'
    on the January statement. Billing month = submission month - 1."""
    match = _EINREICHUNG.search(first_page_text)
    if not match:
        return None
    sub_month, sub_year = int(match.group(2)), int(match.group(3))
    month = sub_month - 1
    year = sub_year
    if month == 0:
        month = 12
        year -= 1
    return f"{month:02d}{year}"


def _source_total(first_page_text: str) -> dict | None:
    match = _BELEGE_SUMMARY.search(first_page_text)
    if not match:
        return None
    return {
        "submitted_invoices_count": int(match.group(1)),
        "submitted_invoices_amount": GermanDecimalParser.parse(match.group(2)),
    }


def extract_lines_from_pdf(path: Path) -> list[dict]:
    """Deterministic extraction of every '<qty> <code> <description>' service
    line followed by two German-decimal amount lines (unit price, line total).

    This pattern only matches inside the 'Abgerechnete Belege' service-line
    sections of the PDF; the Absetzungen/Korrekturen summary sections use a
    different (date-led) line layout and never match, so no section-boundary
    tracking is needed.
    """
    doc = fitz.open(path)
    lines = []
    for page_index in range(len(doc)):
        page_number = page_index + 1
        raw_lines = [ln.strip() for ln in doc[page_index].get_text().splitlines()]
        i, n = 0, len(raw_lines)
        while i < n:
            match = _SERVICE_LINE.match(raw_lines[i])
            if (
                match
                and i + 2 < n
                and _AMOUNT_LINE.match(raw_lines[i + 1])
                and _AMOUNT_LINE.match(raw_lines[i + 2])
            ):
                qty_str, code, description = match.groups()
                lines.append({
                    "page": page_number,
                    "service_code": code,
                    "service_description": description.strip(),
                    "quantity": float(qty_str),
                    "unit_price": GermanDecimalParser.parse(raw_lines[i + 1]),
                    "line_total": GermanDecimalParser.parse(raw_lines[i + 2]),
                })
                i += 3
                continue
            i += 1
    return lines


def build_audit_for_file(path: Path, year: int, org_id: str = DEFAULT_ORG_ID) -> tuple[dict, list[dict]]:
    """Extract one PDF into (document, lines) without touching the database."""
    doc = fitz.open(path)
    first_page_text = doc[0].get_text() if len(doc) else ""

    billing_month = _detected_billing_month(first_page_text)
    source_total = _source_total(first_page_text)
    lines = extract_lines_from_pdf(path)

    extracted_total = round(sum(l["line_total"] for l in lines), 2)
    review_flag = billing_month is None or source_total is None
    if source_total is not None:
        # Source total includes non-0101 codes (SGB V, Verhinderungspflege,
        # Entlastungsleistung, Absetzungen/Korrekturen adjustments) that this
        # extractor does not attribute to a Verordnung the same way, so an
        # exact-cent match is not expected; flag only a materially large gap.
        gap = abs(extracted_total - source_total["submitted_invoices_amount"])
        if gap > max(50.0, source_total["submitted_invoices_amount"] * 0.05):
            review_flag = True

    document = {
        "org_id": org_id,
        "year": year,
        "source_file": path.name,
        "file_hash": file_sha256(path),
        "detected_billing_month": billing_month,
        "extraction_status": "ok" if not review_flag else "review",
        "review_flag": review_flag,
        "source_total_amount": source_total["submitted_invoices_amount"] if source_total else None,
        "source_total_count": source_total["submitted_invoices_count"] if source_total else None,
        "extracted_line_count": len(lines),
        "extracted_total_amount": extracted_total,
        "page_count": len(doc),
        "extracted_at": datetime.now(timezone.utc),
    }
    return document, lines


def persist_audit_for_file(path: Path, year: int, org_id: str = DEFAULT_ORG_ID) -> dict:
    """Extract one PDF and (re)write its audit document + lines. Idempotent:
    re-running for the same file replaces its previous lines entirely, so a
    corrected extractor can be re-run without leaving stale rows behind."""
    db = get_database()
    document, lines = build_audit_for_file(path, year, org_id)

    existing = db.pdf_audit_documents.find_one(
        {"org_id": org_id, "year": year, "source_file": path.name}
    )
    document_id = existing["_id"] if existing else generate_id("pdfaudit")
    document["_id"] = document_id

    db.pdf_audit_documents.replace_one(
        {"org_id": org_id, "year": year, "source_file": path.name},
        document,
        upsert=True,
    )
    db.pdf_audit_lines.delete_many({"org_id": org_id, "document_id": document_id})
    if lines:
        for line in lines:
            line["_id"] = generate_id("pdfauditline")
            line["org_id"] = org_id
            line["year"] = year
            line["document_id"] = document_id
            line["source_file"] = path.name
            line["billing_month"] = document["detected_billing_month"]
        db.pdf_audit_lines.insert_many(lines)

    logger.info(
        "PDF audit: %s -> %s lines, billing_month=%s, status=%s",
        path.name, len(lines), document["detected_billing_month"], document["extraction_status"],
    )
    return document


def run_audit_for_year(year: int, directory: Path, org_id: str = DEFAULT_ORG_ID) -> dict:
    """Rebuild the audit table for every PDF in `directory` (non-recursive)."""
    pdf_paths = sorted(directory.glob("*.pdf"))
    documents = [persist_audit_for_file(p, year, org_id) for p in pdf_paths]
    return {
        "year": year,
        "files_processed": len(documents),
        "files_needing_review": sum(1 for d in documents if d["review_flag"]),
        "total_lines": sum(d["extracted_line_count"] for d in documents),
        "documents": documents,
    }
