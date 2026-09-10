import fitz 
import re
import time
import os
import logging
from app.openai_client import extract_structured_data_with_openai, extract_batch_structured_data
import app.database as database
from app.utils.parsing import GermanDecimalParser
from app.utils.patterns import InvoicePatterns
from app.openai_utils import count_tokens
from app.config.parsing import ParsingConfig

logger = logging.getLogger(__name__)

def extract_text_from_pdf(path):
    doc = fitz.open(path)
    full_text = ""
    for page in doc:
        full_text += page.get_text()
    return full_text

def extract_first_page_text(path):
    """Extract text from only the first page of the PDF."""
    doc = fitz.open(path)
    if len(doc) == 0:
        return ""
    first_page = doc[0]
    return first_page.get_text()

def extract_billing_summary(first_page_text):
    """
    Extract billing summary from first page only.
    Looks for line: "Wert eingereichter Belege" followed by count and amount.
    
    Returns dict with submitted_invoices_count and submitted_invoices_amount, or None if not found.
    """
    try:
        # Pattern: "Wert eingereichter Belege" followed by numbers (count and amount in EUR)
        # Example: "Wert eingereichter Belege     107         44.107,58"
        pattern = r"Wert\s+eingereichter\s+Belege\s+(\d+)\s+([\d.,]+)"
        
        match = re.search(pattern, first_page_text)
        if not match:
            logger.warning("Could not find 'Wert eingereichter Belege' line in first page")
            return None
        
        count_str = match.group(1)
        amount_str = match.group(2)
        
        count = int(count_str)
        amount = GermanDecimalParser.parse(amount_str)
        
        logger.info(f"✓ Billing summary extracted: count={count}, amount={amount}")
        
        return {
            "submitted_invoices_count": count,
            "submitted_invoices_amount": amount
        }
    except Exception as e:
        logger.error(f"Error extracting billing summary: {e}")
        return None

def split_into_chunks(text):
    raw_chunks = []
    current = []

    for line in text.splitlines():
        line = line.strip()

        if line.startswith("Verordnung:"):
            if current:
                raw_chunks.append("\n".join(current))
                current = []
        current.append(line)

    if current:
        raw_chunks.append("\n".join(current))

    cleaned_chunks = []
    for chunk in raw_chunks:
        if not chunk.strip().startswith("Verordnung:"):
            continue  

        lines = chunk.splitlines()
        summe_idx = None
        for i, line in enumerate(lines):
            if line.strip().startswith("Summe €"):
                # Check if amounts are on following lines
                next_lines = lines[i+1:i+3]
                if all(InvoicePatterns.EURO_AMOUNT.value.match(ln.strip()) for ln in next_lines):
                    summe_idx = i + 2
                else:
                    # Amounts might be on the same line as "Summe €", so include this line
                    summe_idx = i

        # Include all lines up to and including the summe line (with amounts)
        # Add 1 extra line in case amounts span to next line
        trimmed = lines[:summe_idx+2] if summe_idx is not None else lines

        # Don't try to extract amounts via regex here - let OpenAI extract them from the full text
        # This ensures SGBV and other formats work correctly regardless of PDF structure

        pflegezeitraum_beginn = pflegezeitraum_ende = None
        for line in lines:
            match = InvoicePatterns.CARE_PERIOD.value.search(line)
            if match:
                pflegezeitraum_beginn, pflegezeitraum_ende = match.groups()
                break

        final_text = "\n".join(trimmed)
        # Note: summe_covered and summe_total will be extracted by OpenAI from the text
        final_text += f'\n"invoice": {{\n    "pflegezeitraum_beginn": {pflegezeitraum_beginn},\n    "pflegezeitraum_ende": {pflegezeitraum_ende}\n}}'

        # Ensure each chunk is small enough for the OpenAI model.
        # Use configurable max tokens from ParsingConfig.
        MODEL = "gpt-5-mini-2025-08-07"
        MAX_TOKENS = ParsingConfig.get_max_tokens_per_call()

        try:
            tok_count = count_tokens(final_text, model=MODEL)
        except Exception:
            tok_count = None

        cleaned_chunks.append(final_text)

    return cleaned_chunks


def clean_4064(structured):
    """
    For Entleistung (4064) invoices, set summe_covered based on the total amount.
    Entleistung is capped at 127.35 EUR per month by insurance:
    - If total <= 127.35: summe_covered = total (fully covered)
    - If total > 127.35: summe_covered = 127.35 (patient pays the rest)
    """
    invoice = structured.get("invoice", {})
    patient = structured.get("patient", {})
    care_account = (
        invoice.get("care_account")
        or invoice.get("pflege_konto")
        or patient.get("pflege_konto")
    )

    if str(care_account).strip() == "4064":
        invoice = structured["invoice"]
        summe_total = invoice.get("summe_total", "0")
        
        # Parse the total amount (handle German format: 100,00 or 100.00)
        try:
            total_value = float(summe_total.replace(",", "."))
        except (ValueError, AttributeError):
            total_value = 0.0
        
        # If not extracted, determine covered amount based on total
        if not invoice.get("summe_covered"):
            if total_value <= 127.35:
                # Fully covered if under or equal to 127.35
                invoice["summe_covered"] = summe_total
            else:
                # Capped at 127.35 if over
                invoice["summe_covered"] = "127,35"

def filter_chunks_by_mode(chunks, mode):
    """
    Filter chunks by processing mode:
    - 'sgbxi': Regular SGB XI (excludes 4092, 4050, 4064)
    - 'sgbv': SGB V (Pflegekonto 4092)
    - 'verhinderungspflege': Verhinderungspflege (Pflegekonto 4050)
    - 'entleistung': Entleistung (Pflegekonto 4064)
    """
    filtered = []

    for i, chunk_item in enumerate(chunks):
        chunk_text = chunk_item["text"] if isinstance(chunk_item, dict) else chunk_item
        chunk_lower = chunk_text.lower()
        chunk_id = chunk_item.get("chunk_id") if isinstance(chunk_item, dict) else None

        if mode == "sgbxi":
            # SGB XI = 4030, 4010, 4020, 4062 — exclude 4092 (SGBV), 4064 (Entlastung), 4050 (Verhinderungspflege)
            if any(code in chunk_lower for code in ("pflegekonto: 4092", "pflegekonto: 4064", "pflegekonto: 4050")):
                logger.debug(f"Skipping chunk {i+1} (chunk_id={chunk_id}) — special pflegekonto.")
                continue

        elif mode == "sgbv":
            # Only include Pflegekonto 4092
            if "pflegekonto: 4092" not in chunk_lower:
                logger.debug(f"Skipping chunk {i+1} (chunk_id={chunk_id}) — not pflegekonto 4092 (SGBV).")
                continue

        elif mode == "verhinderungspflege":
            # Only include Pflegekonto 4050
            if "pflegekonto: 4050" not in chunk_lower:
                logger.debug(f"Skipping chunk {i+1} (chunk_id={chunk_id}) — not pflegekonto 4050 (Verhinderungspflege).")
                continue

        elif mode == "entleistung":
            # Only include Pflegekonto 4064
            if "pflegekonto: 4064" not in chunk_lower:
                logger.debug(f"Skipping chunk {i+1} (chunk_id={chunk_id}) — not pflegekonto 4064 (Entleistung).")
                continue

        else:
            logger.error(f"Invalid processing mode: '{mode}' (expected 'sgbxi', 'sgbv', 'verhinderungspflege', or 'entleistung'). Skipping chunk filtering.")
            break

        filtered.append(chunk_item)

    return filtered

def _process_import_records_llm(text_chunks, abrechnungsmonat, record_type, pflegekonto, result):
    from app.import_records import persist_record

    for offset in range(0, len(text_chunks), 10):
        batch = text_chunks[offset:offset + 10]
        texts = [entry["text"] for entry in batch]
        try:
            records = extract_batch_structured_data(texts, retries=2)
        except Exception:
            records = [None] * len(batch)
        if len(records) != len(batch):
            records = [None] * len(batch)
        for entry, record in zip(batch, records):
            try:
                if record is None:
                    record = extract_structured_data_with_openai(entry["text"], retries=1)
                if record is None:
                    raise ValueError("Extraction failed")
                outcome = persist_record(record, entry["chunk_id"], abrechnungsmonat, record_type, pflegekonto)
                result["duplicates" if outcome["duplicate"] else "committed"] += 1
            except Exception as error:
                result["failed"] += 1
                result["failed_ids"].append(entry["chunk_id"])
                result["failures"].append({"chunk_id": entry["chunk_id"], "code": type(error).__name__})
                logger.warning("Import record %s failed (%s)", entry["chunk_id"], type(error).__name__)


def _process_import_records_deterministic(text_chunks, abrechnungsmonat, record_type, pflegekonto, result):
    # Standalone, non-LLM extraction path (see app/deterministic_parser.py).
    # Fails closed: a chunk that doesn't match the known RZH grammar is
    # recorded as a failure exactly like an LLM extraction failure -- it is
    # never silently skipped or guessed.
    from app.deterministic_parser import parse_chunk
    from app.import_records import persist_record

    for entry in text_chunks:
        try:
            record = parse_chunk(entry["text"])
            outcome = persist_record(record, entry["chunk_id"], abrechnungsmonat, record_type, pflegekonto)
            result["duplicates" if outcome["duplicate"] else "committed"] += 1
        except Exception as error:
            result["failed"] += 1
            result["failed_ids"].append(entry["chunk_id"])
            result["failures"].append({"chunk_id": entry["chunk_id"], "code": type(error).__name__})
            logger.warning("Import record %s failed (%s)", entry["chunk_id"], type(error).__name__)


def process_import_records(text_chunks, abrechnungsmonat, record_type=None, pflegekonto=None, engine="llm"):
    from app.utils.validation import validate_month

    if engine not in ("llm", "deterministic"):
        raise ValueError(f"Unknown extraction engine: {engine!r}")

    validate_month(abrechnungsmonat)
    result = {"attempted": len(text_chunks), "committed": 0, "duplicates": 0,
              "failed": 0, "failed_ids": [], "failures": []}
    if engine == "deterministic":
        _process_import_records_deterministic(text_chunks, abrechnungsmonat, record_type, pflegekonto, result)
    else:
        _process_import_records_llm(text_chunks, abrechnungsmonat, record_type, pflegekonto, result)
    return result


def process_import_sgbxi(text_chunks, abrechnungsmonat, engine="llm"):
    return process_import_records(text_chunks, abrechnungsmonat, engine=engine)


def process_import_sgbv(text_chunks, abrechnungsmonat, engine="llm"):
    return process_import_records(text_chunks, abrechnungsmonat, "SGBV", "4092", engine=engine)


def process_import_verhinderungspflege(text_chunks, abrechnungsmonat, engine="llm"):
    return process_import_records(text_chunks, abrechnungsmonat, "Verhinderungspflege", "4050", engine=engine)
