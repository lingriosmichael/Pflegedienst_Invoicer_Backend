import fitz 
import re
import time
import os
import logging
from app.openai_client import extract_structured_data_with_openai
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
                next_lines = lines[i+1:i+3]
                if all(InvoicePatterns.EURO_AMOUNT.value.match(ln.strip()) for ln in next_lines):
                    summe_idx = i + 2
                else:
                    summe_idx = i

        trimmed = lines[:summe_idx+1] if summe_idx is not None else lines

        euro_values = [
            val.strip() for val in trimmed[-2:]
            if InvoicePatterns.EURO_AMOUNT.value.match(val.strip())
        ]

        if len(euro_values) == 2:
            v1, v2 = euro_values
            v1_float = GermanDecimalParser.parse(v1)
            v2_float = GermanDecimalParser.parse(v2)
            if v1_float >= v2_float:
                summe_total = v1
                summe_covered = v2
            else:
                summe_total = v2
                summe_covered = v1
        else:
            summe_total = summe_covered = None

        pflegezeitraum_beginn = pflegezeitraum_ende = None
        for line in lines:
            match = InvoicePatterns.CARE_PERIOD.value.search(line)
            if match:
                pflegezeitraum_beginn, pflegezeitraum_ende = match.groups()
                break

        final_text = "\n".join(trimmed)
        final_text += f'\n"invoice": {{\n    "pflegezeitraum_beginn": {pflegezeitraum_beginn},\n    "pflegezeitraum_ende": {pflegezeitraum_ende},\n    "summe_covered": {summe_covered},\n    "summe_total": {summe_total}\n}}'

        # Ensure each chunk is small enough for the OpenAI model.
        # Use configurable max tokens from ParsingConfig.
        MODEL = "gpt-5-mini-2025-08-07"
        MAX_TOKENS = ParsingConfig.get_max_tokens_per_call()

        try:
            tok_count = count_tokens(final_text, model=MODEL)
        except Exception:
            tok_count = None

        if tok_count and tok_count > MAX_TOKENS:
            # Split by lines into smaller parts until each part is within the token limit.
            lines = final_text.splitlines()
            sub = []
            current = []
            for line in lines:
                current.append(line)
                try_text = "\n".join(current)
                if count_tokens(try_text, model=MODEL) > MAX_TOKENS:
                    # pop last line and push current chunk
                    current.pop()
                    if current:
                        sub.append("\n".join(current))
                    current = [line]

            if current:
                sub.append("\n".join(current))

            for s in sub:
                cleaned_chunks.append(s)
        else:
            cleaned_chunks.append(final_text)

    return cleaned_chunks

def clean_4064(structured):
    if structured["patient"].get("pflege_konto") == "4064":
        structured["invoice"]["summe_covered"] = "127,35"

def filter_chunks_by_mode(chunks, mode):
    filtered = []

    for i, chunk_item in enumerate(chunks):
        chunk_text = chunk_item["text"] if isinstance(chunk_item, dict) else chunk_item
        chunk_lower = chunk_text.lower()
        chunk_id = chunk_item.get("chunk_id") if isinstance(chunk_item, dict) else None

        if mode == "sgbxi":
            if any(code in chunk_lower for code in ("pflegekonto: 4092", "pflegekonto: 4062", "pflegekonto: 4064", "pflegekonto: 4050")):
                logger.debug(f"Skipping chunk {i+1} (chunk_id={chunk_id}) — excluded pflegekonto.")
                continue

        elif mode == "entleistung":
            if "pflegekonto: 4064" not in chunk_lower:
                logger.debug(f"Skipping chunk {i+1} (chunk_id={chunk_id}) — not pflegekonto 4064.")
                continue

            match = re.findall(r"Summe €\n([\d.,]+)\n([\d.,]+)", chunk_text, flags=re.IGNORECASE)
            if match:
                totals = list(map(lambda s: float(s.replace(".", "").replace(",", ".")), match[0]))
                if max(totals) <= 128:
                    logger.debug(f"Skipping chunk {i+1} (chunk_id={chunk_id}) — sum_total ≤ 128 EUR.")
                    continue

        else:
            logger.error(f"Invalid processing mode: '{mode}' (expected 'sgbxi' or 'entleistung'). Skipping chunk filtering.")
            break

        filtered.append(chunk_item)

    return filtered

def process_import_sgbxi(text_chunks, abrechnungsmonat):
    inserted = 0

    for i, chunk_entry in enumerate(text_chunks):
        chunk_text = chunk_entry["text"] if isinstance(chunk_entry, dict) else chunk_entry
        chunk_id = chunk_entry.get("chunk_id") if isinstance(chunk_entry, dict) else None
        logger.info(f"Processing chunk {i+1}{f' (chunk_id={chunk_id})' if chunk_id else ''}")
        print("Chunk:")
        print(chunk_text)

        try:
            structured = extract_structured_data_with_openai(chunk_text, retries=1)
            print("\nStructured output:")
            print(structured)

            if not structured:
                logger.warning(f"Structured extraction returned no data for chunk {i+1}{f' (chunk_id={chunk_id})' if chunk_id else ''}")
                continue

            patient = structured.get("patient", {})
            invoice = structured.get("invoice", {})
            name = patient.get("name", "[unknown]")

            if not invoice or "summe_covered" not in invoice or "summe_total" not in invoice:
                logger.warning(f"Missing invoice totals for {name}. Retrying with increased retries...")
                structured_retry = extract_structured_data_with_openai(chunk_text, retries=2)
                invoice_retry = structured_retry.get("invoice", {}) if structured_retry else {}

                if "summe_covered" in invoice_retry and "summe_total" in invoice_retry:
                    structured = structured_retry
                    invoice = invoice_retry
                    logger.info(f"Retry successful for {name}: found summe_covered={invoice.get('summe_covered')}, summe_total={invoice.get('summe_total')}")
                else:
                    logger.error(f"Retry failed for {name}: Could not extract summe_covered and summe_total after 2 retries. Skipping.")
                    continue

            if not patient.get("birthdate"):
                print(chunk_text)
                logger.warning(f"Patient {name} (insurance_number={patient.get('insurance_number')}) is missing birthdate. Skipping this entry as auto-fix is not available in API mode.")

            structured["invoice"]["abrechnungsmonat"] = abrechnungsmonat
            clean_4064(structured)

            for attempt in range(3):
                try:
                    database.insert_structured_data(structured, origin_chunk_id=chunk_id)
                    logger.info(f"✓ Successfully inserted: {patient.get('insurance_number')} ({name})")
                    inserted += 1

                    if chunk_id:
                        try:
                            database.update_chunk_patient(chunk_id, patient.get("name"))
                        except Exception:
                            logger.exception(f"Failed to link chunk {chunk_id} with patient metadata")
                    break
                except Exception as e:
                    if "database is locked" in str(e).lower():
                        wait_time = 2 ** attempt
                        logger.warning(f"Database locked during insert for {name}. Retrying in {wait_time}s... (attempt {attempt + 1}/3)")
                        time.sleep(wait_time)
                    else:
                        logger.error(f"Failed to insert structured data for {name} (insurance_number={patient.get('insurance_number')}): {type(e).__name__}: {e}")
                        break
        except Exception as e:
            logger.error(f"Unexpected error processing chunk {i+1}{f' (chunk_id={chunk_id})' if chunk_id else ''}: {type(e).__name__}: {e}")

    logger.info(f"Inserted: {inserted}")

def refeed_failed_chunk_from_file():
    chunk_path = "logs/failed.txt"
    logger.info(f"Re-processing chunk from {chunk_path}")

    if not os.path.exists(chunk_path):
        logger.error("File not found.")
        return

    with open(chunk_path, "r", encoding="utf-8") as f:
        chunk_text = f.read()

    logger.info("Re-processing the following chunk...")
    structured = extract_structured_data_with_openai(chunk_text, retries=2)
    if not structured:
        logger.error("Failed to extract structured data.")
        return

    logger.info("Structured output retrieved.")
    insert_structured_data(structured)
    logger.info("Inserted successfully.")
    

_temp_chunks_cache = {}

def store_chunks_temp(month: str, chunks: list):
    """
    Store extracted chunks temporarily in memory.
    Used by /prepare_pdf so /process_pdf can reuse them.
    """
    _temp_chunks_cache[month] = chunks
    logger.debug(f"Cached {len(chunks)} chunks for {month}")


def get_chunks_temp(month: str):
    """
    Retrieve cached chunks if available.
    """
    return _temp_chunks_cache.get(month)


def clear_chunks_temp(month: str):
    """
    Optional: Clear cached chunks after processing.
    """
    if month in _temp_chunks_cache:
        del _temp_chunks_cache[month]
        logger.debug(f"Cleared chunk cache for {month}")
