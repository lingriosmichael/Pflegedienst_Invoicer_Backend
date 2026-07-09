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

def generate_random_billing_summary():
    """Generate random billing summary data for testing/demo purposes."""
    import random
    count = random.randint(5, 50)
    amount = random.uniform(500, 10000)
    
    return {
        "submitted_invoices_count": count,
        "submitted_invoices_amount": round(amount, 2)
    }


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

def process_import_sgbxi(text_chunks, abrechnungsmonat):
    inserted = 0
    BATCH_SIZE = 10
    
    # Group chunks into batches
    batches = []
    for i in range(0, len(text_chunks), BATCH_SIZE):
        batches.append(text_chunks[i:i + BATCH_SIZE])
    
    logger.info(f"Processing {len(text_chunks)} chunks in {len(batches)} batch(es) of up to {BATCH_SIZE}")
    
    for batch_idx, batch in enumerate(batches, 1):
        logger.info(f"\n{'='*80}")
        logger.info(f"Processing Batch {batch_idx}/{len(batches)} ({len(batch)} chunks)")
        logger.info(f"{'='*80}")
        
        # Extract chunk texts and metadata
        chunk_texts = []
        chunk_metadata = []
        
        for chunk_entry in batch:
            chunk_text = chunk_entry["text"] if isinstance(chunk_entry, dict) else chunk_entry
            chunk_id = chunk_entry.get("chunk_id") if isinstance(chunk_entry, dict) else None
            chunk_texts.append(chunk_text)
            chunk_metadata.append({"chunk_id": chunk_id, "chunk_entry": chunk_entry})
        
        # Get batch extraction results
        try:
            batch_results = extract_batch_structured_data(chunk_texts, retries=2)
            # batch_results is a LIST of structured invoice objects
            # One object per chunk: [invoice1, invoice2, invoice3, ...]
        except Exception as e:
            logger.error(f"Batch extraction failed: {e}. Falling back to individual extraction...")
            # Fallback: process individually
            batch_results = []
            for chunk_text in chunk_texts:
                try:
                    result = extract_structured_data_with_openai(chunk_text, retries=1)
                    batch_results.append(result)
                except Exception as e2:
                    logger.error(f"Individual extraction failed: {e2}")
                    batch_results.append(None)
        
        # Process results - iterate through the list of structured objects
        for result_idx, (structured, metadata) in enumerate(zip(batch_results, chunk_metadata), 1):
            chunk_id = metadata["chunk_id"]
            chunk_entry = metadata["chunk_entry"]
            chunk_text = chunk_entry["text"] if isinstance(chunk_entry, dict) else chunk_entry
            global_idx = (batch_idx - 1) * BATCH_SIZE + result_idx
            
            # Log SGBV OpenAI responses
            if "pflegekonto: 4092" in chunk_text.lower():
                print(f"\n[SGBV OpenAI RESULT] Chunk ID: {chunk_id}")
                print(f"Raw response: {structured}")
                if structured:
                    invoice = structured.get("invoice", {})
                    print(f"Invoice section: summe_covered={invoice.get('summe_covered')}, summe_total={invoice.get('summe_total')}")
            
            print(f"\n--- Result {result_idx}/{len(batch)} (Global: {global_idx}) ---")
            print(f"Chunk ID: {chunk_id}")
            
            if not structured:
                logger.warning(f"Batch result {result_idx}: No structured data extracted for chunk {chunk_id}")
                continue
            
            print(f"Structured output: {structured}")
            
            patient = structured.get("patient", {})
            invoice = structured.get("invoice", {})
            name = patient.get("name", "[unknown]")
            
            # Validate required fields
            if not invoice or "summe_covered" not in invoice or "summe_total" not in invoice:
                is_sgbv = "pflegekonto: 4092" in chunk_text.lower()
                if is_sgbv:
                    print(f"\n[SGBV MISSING AMOUNTS] Chunk ID: {chunk_id}, Retrying individually...")
                logger.warning(f"Batch result {result_idx}: Missing invoice totals for {name}. Retrying individually...")
                chunk_text = chunk_entry["text"] if isinstance(chunk_entry, dict) else chunk_entry
                try:
                    structured_retry = extract_structured_data_with_openai(chunk_text, retries=2)
                    if structured_retry:
                        invoice_retry = structured_retry.get("invoice", {})
                        if "summe_covered" in invoice_retry and "summe_total" in invoice_retry:
                            structured = structured_retry
                            invoice = invoice_retry
                            if is_sgbv:
                                print(f"[SGBV RETRY SUCCESS] Found: summe_covered={invoice.get('summe_covered')}, summe_total={invoice.get('summe_total')}")
                            logger.info(f"Individual retry successful for {name}: found summe_covered={invoice.get('summe_covered')}, summe_total={invoice.get('summe_total')}")
                        else:
                            if is_sgbv:
                                print(f"[SGBV RETRY FAILED] Still missing amounts after retry. Invoice: {invoice_retry}")
                            logger.error(f"Individual retry failed for {name}: Missing required fields. Skipping.")
                            continue
                    else:
                        if is_sgbv:
                            print(f"[SGBV RETRY FAILED] No response from OpenAI on individual retry")
                        logger.error(f"Individual retry failed for {name}: No response. Skipping.")
                        continue
                except Exception as e:
                    if is_sgbv:
                        print(f"[SGBV RETRY ERROR] {e}")
                    logger.error(f"Individual retry error for {name}: {e}. Skipping.")
                    continue
            
            if not patient.get("birthdate"):
                logger.warning(f"Batch result {result_idx}: Patient {name} (insurance_number={patient.get('insurance_number')}) missing birthdate. Skipping.")
                continue
            
            structured["invoice"]["abrechnungsmonat"] = abrechnungsmonat
            clean_4064(structured)
            
            # Insert individual result into database
            # Each structured object from the batch_results list is inserted separately
            for attempt in range(3):
                try:
                    database.insert_structured_data(structured, origin_chunk_id=chunk_id, invoicing_month=abrechnungsmonat)
                    logger.info(f"✓ Batch {batch_idx}, Result {result_idx}/{len(batch)}: Successfully inserted {patient.get('insurance_number')} ({name})")
                    inserted += 1
                    break
                except Exception as e:
                    if attempt < 2:
                        logger.warning(f"Attempt {attempt + 1} failed to insert: {e}. Retrying...")
                    else:
                        logger.error(f"Failed to insert after 3 attempts: {e}")
    
    logger.info(f"\n{'='*80}")
    logger.info(f"✅ Batch processing complete. Inserted {inserted}/{len(text_chunks)} records.")
    logger.info(f"{'='*80}\n")


def _process_batch_sequentially(batch_chunks, batch_idx, total_batches, record_type, pflegekonto):
    """
    Extract a batch from OpenAI, then insert all records sequentially.
    Returns number of successfully inserted records.
    
    Args:
        batch_chunks: List of text chunks for this batch
        batch_idx: Current batch number (for logging)
        total_batches: Total number of batches (for logging)
        record_type: 'SGBV' or 'Verhinderungspflege'
        pflegekonto: Care account code (4092 or 4050)
    """
    inserted = 0
    
    # Step 1: Extract batch from OpenAI
    logger.info(f"{record_type} Batch {batch_idx}/{total_batches} ({len(batch_chunks)} chunks)")
    
    chunk_texts = []
    chunk_metadata = []
    
    for chunk_entry in batch_chunks:
        chunk_text = chunk_entry["text"] if isinstance(chunk_entry, dict) else chunk_entry
        chunk_id = chunk_entry.get("chunk_id") if isinstance(chunk_entry, dict) else None
        chunk_texts.append(chunk_text)
        chunk_metadata.append({"chunk_id": chunk_id, "chunk_entry": chunk_entry})
    
    try:
        batch_results = extract_batch_structured_data(chunk_texts, retries=2)
    except Exception as e:
        logger.error(f"{record_type} batch extraction failed: {e}. Falling back to individual extraction...")
        batch_results = []
        for chunk_text in chunk_texts:
            try:
                result = extract_structured_data_with_openai(chunk_text, retries=1)
                batch_results.append(result)
            except Exception as e2:
                logger.error(f"Individual extraction failed: {e2}")
                batch_results.append(None)
    
    logger.info(f"{record_type} extraction complete, starting inserts...")
    
    # Step 2: Insert all results from this batch sequentially
    # Retry loop for database lock issues
    max_insert_retries = 3
    for insert_attempt in range(max_insert_retries):
        try:
            for result_idx, (structured, metadata) in enumerate(zip(batch_results, chunk_metadata), 1):
                chunk_id = metadata["chunk_id"]
                
                if not structured:
                    logger.warning(f"{record_type} result {result_idx}: No structured data for chunk {chunk_id}")
                    continue
                
                patient = structured.get("patient", {})
                invoice = structured.get("invoice", {})
                name = patient.get("name", "[unknown]")
                
                if not patient.get("birthdate"):
                    logger.warning(f"{record_type} result {result_idx}: Patient {name} missing birthdate. Skipping.")
                    continue
                
                try:
                    record_id = database.insert_care_record(
                        data=structured,
                        record_type=record_type,
                        pflegekonto=pflegekonto,
                        origin_chunk_id=chunk_id
                    )
                    if record_id:
                        logger.info(f"✓ {record_type} Batch {batch_idx}, Result {result_idx}: Inserted {name} (record_id={record_id})")
                        inserted += 1
                except Exception as e:
                    logger.error(f"{record_type} insertion failed for {name}: {e}")
            
            # Success - break out of retry loop
            break
            
        except Exception as batch_error:
            logger.error(f"{record_type} batch insert attempt {insert_attempt + 1}/{max_insert_retries} failed: {batch_error}")
            if insert_attempt < max_insert_retries - 1:
                import time
                wait_time = (2 ** insert_attempt) * 1.0  # 1s, 2s, 4s
                logger.warning(f"Retrying in {wait_time}s...")
                time.sleep(wait_time)
            else:
                logger.error(f"{record_type} batch insert failed after {max_insert_retries} attempts")
    
    return inserted


def process_import_sgbv(text_chunks, abrechnungsmonat):
    """Process SGBV care records (Pflegekonto 4092) - non-billable data collection.
    
    Sequential flow: Extract batch → Insert all → Request next batch
    """
    total_inserted = 0
    BATCH_SIZE = 10
    
    # Group chunks into batches
    batches = []
    for i in range(0, len(text_chunks), BATCH_SIZE):
        batches.append(text_chunks[i:i + BATCH_SIZE])
    
    logger.info(f"\n{'='*80}")
    logger.info(f"Processing {len(text_chunks)} SGBV chunks in {len(batches)} batch(es)")
    logger.info(f"{'='*80}")
    
    # Process each batch sequentially: extract then insert
    for batch_idx, batch in enumerate(batches, 1):
        inserted = _process_batch_sequentially(
            batch_chunks=batch,
            batch_idx=batch_idx,
            total_batches=len(batches),
            record_type="SGBV",
            pflegekonto="4092"
        )
        total_inserted += inserted
    
    logger.info(f"✅ SGBV processing complete. Inserted {total_inserted}/{len(text_chunks)} records.\n")


def process_import_verhinderungspflege(text_chunks, abrechnungsmonat):
    """Process Verhinderungspflege care records (Pflegekonto 4050) - non-billable data collection.
    
    Sequential flow: Extract batch → Insert all → Request next batch
    """
    total_inserted = 0
    BATCH_SIZE = 10
    
    # Group chunks into batches
    batches = []
    for i in range(0, len(text_chunks), BATCH_SIZE):
        batches.append(text_chunks[i:i + BATCH_SIZE])
    
    logger.info(f"\n{'='*80}")
    logger.info(f"Processing {len(text_chunks)} Verhinderungspflege chunks in {len(batches)} batch(es)")
    logger.info(f"{'='*80}")
    
    # Process each batch sequentially: extract then insert
    for batch_idx, batch in enumerate(batches, 1):
        inserted = _process_batch_sequentially(
            batch_chunks=batch,
            batch_idx=batch_idx,
            total_batches=len(batches),
            record_type="Verhinderungspflege",
            pflegekonto="4050"
        )
        total_inserted += inserted
    
    logger.info(f"✅ Verhinderungspflege processing complete. Inserted {total_inserted}/{len(text_chunks)} records.\n")


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
