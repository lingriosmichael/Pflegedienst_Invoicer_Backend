from openai import OpenAI
import re
import json
import os
import logging
from app.db.mongodb_config import get_database
from app.core.config import settings
from app.openai_utils import make_cache_key, cache_failed_request, count_tokens

logger = logging.getLogger(__name__)

# JSON Schema for single invoice object (used for both single and batch responses)
INVOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "patient": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "birthdate": {"type": "string"},
                "insurance_number": {"type": "string"},
                "care_level": {"type": "string"},
                "pflege_konto": {"type": "string"}
            },
            "required": ["name", "birthdate", "insurance_number", "care_level", "pflege_konto"],
            "additionalProperties": False
        },
        "services": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "quantity": {"type": "string"},
                    "code": {"type": "string"},
                    "description": {"type": "string"},
                    "unit_price": {"type": "string"},
                    "total_price": {"type": "string"}
                },
                "required": ["quantity", "code", "description", "unit_price", "total_price"],
                "additionalProperties": False
            }
        },
        "invoice": {
            "type": "object",
            "properties": {
                "pflegezeitraum_beginn": {"type": "string"},
                "pflegezeitraum_ende": {"type": "string"},
                "summe_covered": {"type": "string"},
                "summe_total": {"type": "string"}
            },
            "required": ["pflegezeitraum_beginn", "pflegezeitraum_ende", "summe_covered", "summe_total"],
            "additionalProperties": False
        }
    },
    "required": ["patient", "services", "invoice"],
    "additionalProperties": False
}

# Batch schema: object wrapper with array of invoices (required by OpenAI json_schema which needs type: "object")
BATCH_INVOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "invoices": {
            "type": "array",
            "items": INVOICE_SCHEMA
        }
    },
    "required": ["invoices"],
    "additionalProperties": False
}

# Create OpenAI client using configured API key (from env/.env). Do NOT keep keys in source.
client = None
if not settings.OPENAI_API_KEY:
  logger.warning("OPENAI_API_KEY is not set. Set OPENAI_API_KEY in the environment or .env file.")
else:
  try:
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
  except Exception as e:
    logger.warning(f"Failed to initialize OpenAI client: {e}")

LOG_PATH = "logs/invalid_json.log"
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

def safe_parse_json(text):
    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if not json_match:
        msg = f"Could not find JSON in response:\n{text}\n{'-'*80}\n"
        logger.error(msg)
        with open(LOG_PATH, "a") as f:
            f.write("NO JSON FOUND:\n" + msg)
        return None

    json_text = json_match.group(0).strip()

    try:
        return json.loads(json_text)
    except json.JSONDecodeError as e:
        msg = f"Invalid JSON:\n{json_text}\nError: {e}\n{'-'*80}\n"
        logger.error(msg)
        with open(LOG_PATH, "a") as f:
            f.write("JSON DECODE ERROR:\n" + msg)
        return None

def build_prompt(attempt=0):
    base_prompt = '''
      You are an expert at reading German Pflegedienst invoices.

      Given the following billing block, extract structured JSON with:

      1. Patient details
      2. A list of ALL services in the block (each with quantity, code, description, unit price, total)
      3. Invoice object to be copied as it is.
      4. Note: All monetary values must be formatted in German style, using ',' for decimals and '.' for thousands (e.g. "1.234,56").
         That means: unit price, total, summe_covered, summe_total
      
      CRITICAL: After "Summe €" there are ALWAYS exactly two numbers:
      - FIRST number = summe_total (total service costs)
      - SECOND number = summe_covered (insurance covered amount)
      
      Example from Kostenträger format:
      Summe €
      2.148,60        (FIRST = summe_total)
      1.859,00        (SECOND = summe_covered)
      
      Types of Sample block:
      Verordnung: 01.03.25 
      Patient: MÜLLER, KARL - 06.01.41 - L011897478 - Status: 10001 - Pflegegrad: 4
      Verordnungsdatum: 01.03.25, Pflegezeitraum: 01.03.25 - 31.03.25, Pflegekonto: 4030
      Anz Position
      E-Preis €
      Summe €
      MwSt%
      Zuzahlung €
      31 0101002a Kleine Morgen/Abendtoilette mit
      15,41
      477,71
      31 01010003 Grosse Morgen/Abendtoilette mit
      42,57
      1.319,67
      1 01013021 Ausbildungspauschale nach § 26 PflBG
      61,23
      61,23
      Summe 
      1.758,61
      1.858,61
      "invoice": {
          "pflegezeitraum_beginn": "01.03.25",
          "pflegezeitraum_ende": "31.03.25",
          "summe_covered": "1.758,61",
          "summe_total": "1.858,61"
        }

      Expected JSON format:
      {{
        "patient": {{
          "name": "Müller, Karl",
          "birthdate": "06.01.41",
          "insurance_number": "L011897478",
          "care_level": "4",
          "pflege_konto": "4030"
        }},
        "services": [
          {{
            "quantity": "31",
            "code": "0101002a",
            "description": "Kleine Morgen/Abendtoilette mit",
            "unit_price": "15,41",
            "total_price": "477,71"
          }},
          {{
            "quantity": "31",
            "code": "01010003",
            "description": "Grosse Morgen/Abendtoilette mit",
            "unit_price": "42,57",
            "total_price": "1.319,67"
          }},
          {{
            "quantity": "1",
            "code": "01013021",
            "description": "Ausbildungspauschale nach § 26 PflBG",
            "unit_price": "61,23",
            "total_price": "61,23"
          }}
        ],
        "invoice": {{
          "pflegezeitraum_beginn": "01.03.25",
          "pflegezeitraum_ende": "31.03.25",
          "summe_covered": "1.758,61",
          "summe_total": "1.858,61"
        }}
      }}

      Another sample:

      Verordnung: 01.03.25 - Pflegezeitraum: 07.03.25 - 25.03.25
      Patient: DIENST, UTE - 05.03.58 - P708479787 - Status: 10001 - Pflegegrad: 3
      Verordnungsdatum: 01.03.25, Pflegezeitraum: 07.03.25 - 25.03.25, Pflegekonto: 4020
      Anz Position
      E-Preis €
      Summe €
      MwSt%
      Zuzahlung €
      4 01010003 Grosse Morgen/Abendtoilette mit
      42,57
      170,28
      1 01013021 Ausbildungspauschale nach § 26 PflBG
      5,80
      5,80
      Summe €
      176,08
      176,08

      "invoice": {
          "pflegezeitraum_beginn": "07.03.25",
          "pflegezeitraum_ende": "25.03.25",
          "summe_covered": "176,08",
          "summe_total": "176,08"
        }

      Expected JSON format:
      {{
        "patient": {{
          "name": "Dienst, Ute",
          "birthdate": "05.03.58",
          "insurance_number": "P708479787",
          "care_level": "3",
          "pflege_konto": "4020"
        }},
        "services": [
          {{
            "quantity": "4",
            "code": "01010003",
            "description": "Grosse Morgen/Abendtoilette mit",
            "unit_price": "42,57",
            "total_price": "170,28"
          }},
          {{
            "quantity": "1",
            "code": "01013021",
            "description": "Ausbildungspauschale nach § 26 PflBG",
            "unit_price": "5,80",
            "total_price": "5,80"
          }}
        ],
        "invoice": {{
          "pflegezeitraum_beginn": "07.03.25",
          "pflegezeitraum_ende": "25.03.25",
          "summe_covered": "176,08",
          "summe_total": "176,08"
        }}
      }}

      Now process this block and return ONLY valid JSON5 with no comments, explanations, or markdown formatting.
      '''

    if attempt > 0:
        base_prompt += "\n\n⚠️ This is a retry. Ensure JSON is well-formed and contains no comments."

    return base_prompt

def extract_structured_data_with_openai(chunk_text, retries=2):
  MODEL = "gpt-5-mini"
  # Assumed model token limit for planning. If you know the exact limit for the model,
  # you can control limits via ParsingConfig.get_max_tokens_per_call() in pdf parsing.

  for attempt in range(retries + 1):
    system_prompt = build_prompt(attempt)

    # Token counting (best-effort)
    try:
      token_count = count_tokens(system_prompt + "\n" + chunk_text, model=MODEL)
      logger.debug(f"Token estimate for request: {token_count} tokens (model={MODEL})")
    except Exception:
      token_count = None

    # Debug log: Check if chunk has SGBV and amounts
    if "pflegekonto: 4092" in chunk_text.lower():
      logger.info("=" * 80)
      logger.info("[SGBV OpenAI] Received chunk with SGBV")
      logger.info(f"[SGBV OpenAI] Chunk (first 1000 chars):\n{chunk_text[:1000]}")
      logger.info("=" * 80)

    try:
      rsp = client.chat.completions.create(
        model=MODEL,
        messages=[
          {"role": "system", "content": system_prompt},
          {"role": "user", "content": chunk_text},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "invoice_record",
                "description": "Single extracted invoice record",
                "schema": INVOICE_SCHEMA,
                "strict": True
            }
        }
      )

      content = rsp.choices[0].message.content
      parsed = json.loads(content)
      
      # Debug log for SGBV - log full response
      care_account = parsed.get("invoice", {}).get("care_account", "")
      if care_account == "4092" or "4092" in str(parsed):
          logger.info("=" * 80)
          logger.info("[SGBV OpenAI Response]")
          logger.info(f"[SGBV Response] Full parsed JSON:\n{json.dumps(parsed, indent=2, ensure_ascii=False)}")
          logger.info(f"[SGBV Response] summe_covered: {parsed['invoice'].get('summe_covered')}")
          logger.info(f"[SGBV Response] summe_total: {parsed['invoice'].get('summe_total')}")
          logger.info("=" * 80)
      
      return parsed

    except Exception as e:
      error_msg = str(e)
      logger.error(f"OpenAI API error (attempt {attempt + 1}/{retries + 1}): {error_msg}")
      
      # Cache the failed request for troubleshooting
      cache_failed_request(MODEL, system_prompt, chunk_text, error_msg)
      
      if attempt < retries:
        logger.info(f"🔁 Retrying... ({attempt + 1}/{retries})")
      else:
        logger.error(f"❌ All {retries + 1} attempts failed for chunk")
  
  return None


def extract_batch_structured_data(chunk_texts: list, retries=2):
  """
  Extract structured data from a batch of chunks in a single API call.
  
  Args:
    chunk_texts: List of chunk text strings (max 10 recommended)
    retries: Number of retry attempts
    
  Returns:
    List of structured data objects, one per chunk. Failed chunks return None.
  """
  MODEL = "gpt-5-mini"
  
  if not chunk_texts:
    return []
  
  # Build combined input with clear separators
  batch_input = ""
  for i, chunk_text in enumerate(chunk_texts, 1):
    batch_input += f"\n{'='*80}\nCHUNK {i}:\n{'='*80}\n{chunk_text}\n"
  
  for attempt in range(retries + 1):
    system_prompt = f"""{build_prompt(attempt)}

BATCH PROCESSING MODE:
You will receive multiple billing blocks separated by "====" lines.
For EACH block:
1. Extract its structured JSON following the exact schema provided
2. Return a JSON object with an "invoices" array containing one object per chunk

CRITICAL REQUIREMENTS:
- Return ONLY a valid JSON object with "invoices" key containing an array
- Each object in the array MUST have exactly these fields in this order: "patient", "services", "invoice"
- Patient object MUST have: name, birthdate, insurance_number, care_level, pflege_konto
- Services array MUST contain objects with: quantity, code, description, unit_price, total_price
- Invoice object MUST have: pflegezeitraum_beginn, pflegezeitraum_ende, summe_covered, summe_total
- All monetary values MUST use German format: "1.234,56" (comma for decimals, dot for thousands)
- If a chunk cannot be parsed, SKIP it entirely (do not include null)
- Return empty invoices array [] if no chunks can be parsed

Return format MUST be:
{{
  "invoices": [
    {{"patient": {{"name": "...", "birthdate": "...", "insurance_number": "...", "care_level": "...", "pflege_konto": "..."}}, "services": [{{"quantity": "...", "code": "...", "description": "...", "unit_price": "...", "total_price": "..."}}], "invoice": {{"pflegezeitraum_beginn": "...", "pflegezeitraum_ende": "...", "summe_covered": "...", "summe_total": "..."}}}},
    ...
  ]
}}

Process all chunks sequentially in the same request.
"""

    # Token counting (best-effort)
    try:
      token_count = count_tokens(system_prompt + batch_input, model=MODEL)
      logger.debug(f"Batch token estimate: {token_count} tokens (model={MODEL}, chunks={len(chunk_texts)})")
    except Exception:
      pass

    try:
      rsp = client.chat.completions.create(
        model=MODEL,
        messages=[
          {"role": "system", "content": system_prompt},
          {"role": "user", "content": batch_input},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "batch_invoices",
                "description": "Array of extracted invoice records",
                "schema": BATCH_INVOICE_SCHEMA,
                "strict": True
            }
        }
      )

      content = rsp.choices[0].message.content
      parsed = json.loads(content)
      
      # Debug logging for batch results
      print(f"\n[BATCH RESPONSE] Raw content (first 1000 chars):\n{content[:1000]}")
      if any("4092" in ct for ct in chunk_texts):
        print(f"[BATCH RESPONSE] Has SGBV chunks. Full parsed:\n{parsed}")

      # Ensure we always return a list
      if isinstance(parsed, dict) and "invoices" in parsed:
        results = parsed["invoices"]
      elif isinstance(parsed, list):
        results = parsed
      else:
        results = [parsed] if parsed else []

      # VALIDATION: Check that we got results for each chunk
      # This helps detect if LLM mixed data from multiple chunks
      valid_results = [r for r in results if r is not None]
      
      if len(valid_results) != len(chunk_texts):
        logger.warning(f"⚠️ Result count mismatch! Sent {len(chunk_texts)} chunks, got {len(valid_results)} results. Some chunks may have failed parsing.")
      
      # Log chunk-by-chunk mapping for verification
      for idx, (chunk_text, result) in enumerate(zip(chunk_texts, results), 1):
        if result:
          patient_name = result.get("patient", {}).get("name", "UNKNOWN")
          insurance = result.get("patient", {}).get("insurance_number", "UNKNOWN")
          logger.debug(f"CHUNK {idx} → Patient: {patient_name} (Insurance: {insurance})")
        else:
          logger.debug(f"CHUNK {idx} → Failed/Skipped")

      logger.info(f"✅ Batch extraction successful: {len(valid_results)}/{len(chunk_texts)} chunks extracted")
      return results

    except Exception as e:
      error_msg = str(e)
      logger.error(f"Batch OpenAI API error (attempt {attempt + 1}/{retries + 1}): {error_msg}")
      
      # Cache the failed batch request for troubleshooting
      cache_failed_request(MODEL, system_prompt, batch_input, error_msg)
      logger.error(f"Batch OpenAI API error: {e}")
      if attempt < retries:
        print(f"🔁 Batch retry {attempt + 1}...")
  
  logger.error(f"Batch extraction failed after {retries + 1} attempts")
  return [None] * len(chunk_texts)

