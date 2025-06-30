import fitz 
import re
import time
import os
from app.openai import extract_structured_data_batch 
from app.database import insert_structured_data


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
                if all(re.match(r"^\d{1,3}(\.\d{3})?,\d{2}$", l.strip()) for l in next_lines):
                    summe_idx = i + 2
                else:
                    summe_idx = i

        trimmed = lines[:summe_idx+1] if summe_idx is not None else lines

        euro_values = [
            l.strip() for l in trimmed[-2:]
            if re.match(r"^\d{1,3}(\.\d{3})?,\d{2}$", l.strip())
        ]

        if len(euro_values) == 2:
            v1, v2 = euro_values
            v1_float = float(v1.replace('.', '').replace(',', '.'))
            v2_float = float(v2.replace('.', '').replace(',', '.'))
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
            match = re.search(r"Pflegezeitraum:\s*(\d{2}\.\d{2}\.\d{2})\s*-\s*(\d{2}\.\d{2}\.\d{2})", line)
            if match:
                pflegezeitraum_beginn, pflegezeitraum_ende = match.groups()
                break

        final_text = "\n".join(trimmed)
        final_text += f'\n"invoice": {{\n    "pflegezeitraum_beginn": {pflegezeitraum_beginn},\n    "pflegezeitraum_ende": {pflegezeitraum_ende},\n    "summe_covered": {summe_covered},\n    "summe_total": {summe_total}\n}}'

        cleaned_chunks.append(final_text)

    return cleaned_chunks

def clean_4064(structured):
    if structured["patient"].get("pflege_konto") == "4064":
        structured["invoice"]["summe_covered"] = "127,35"

def process_import(text_chunks, abrechnungsmonat):
    inserted = 0

    print(f"📦 Processing {len(text_chunks)} chunks...")
    structured_list = extract_structured_data_batch(text_chunks)
    if not structured_list:
        print("❌ GPT extraction failed.")
        return

    for structured in structured_list:
        try:
            patient = structured.get("patient", {})
            invoice = structured.get("invoice", {})
            name = patient.get("name", "[unknown]")

            if not invoice or "summe_covered" not in invoice or "summe_total" not in invoice:
                print(f"⚠️ Missing invoice totals for {name}. Skipping...")
                continue

            if not patient.get("birthdate"):
                print(f"⚠️ Patient {name} is missing birthdate.")
                print("⛔️ Skipping due to missing birthdate.")
                continue

            structured["invoice"]["abrechnungsmonat"] = abrechnungsmonat
            clean_4064(structured)

            for attempt in range(3):
                try:
                    insert_structured_data(structured)
                    print(f"✅ Inserted structured data for {patient.get('insurance_number')}")
                    inserted += 1
                    break
                except Exception as e:
                    if "database is locked" in str(e).lower():
                        wait_time = 2 ** attempt
                        print(f"⏳ DB locked. Retrying in {wait_time}s...")
                        time.sleep(wait_time)
                    else:
                        print(f"❌ Insert failed: {e}")
                        input("🔍 Press Enter to continue...")
                        break

        except Exception as e:
            print(f"❌ Unexpected error for {name}: {e}")
            input("🔍 Press Enter to continue...")

    print(f"\n✅ Inserted: {inserted}")

def refeed_failed_chunk_from_file():
    chunk_path = "logs/failed.txt"
    print(f"🔁 Re-processing chunk from {chunk_path}")

    if not os.path.exists(chunk_path):
        print("❌ File not found.")
        return

    with open(chunk_path, "r", encoding="utf-8") as f:
        chunk_text = f.read()

    print("\n📦 Re-processing the following chunk:\n")
    print(chunk_text)

    structured_list = extract_structured_data_batch([chunk_text])
    if not structured_list:
        print("❌ Failed to extract structured data.")
        return

    structured = structured_list[0]
    print("\n✅ Structured Output:")
    print(structured)
    insert_structured_data(structured)
    print("✅ Inserted successfully.")
