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
    try:
        return json.loads(text)
    except (ValueError, TypeError):
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

def _extract(schema, prompt, user_text, retries):
    if client is None:
        raise RuntimeError("Extraction service is not configured")
    for attempt in range(retries + 1):
        try:
            response = client.chat.completions.create(
                model="gpt-5-mini",
                messages=[{"role": "system", "content": prompt}, {"role": "user", "content": user_text}],
                response_format={"type": "json_schema", "json_schema": {
                    "name": "invoice_extraction", "schema": schema, "strict": True}},
            )
            return json.loads(response.choices[0].message.content)
        except Exception:
            logger.warning("Extraction attempt %d failed", attempt + 1)
    return None


def extract_structured_data_with_openai(chunk_text, retries=2):
    return _extract(INVOICE_SCHEMA, build_prompt(), chunk_text, retries)


def extract_batch_structured_data(chunk_texts, retries=2):
    if not chunk_texts:
        return []
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"chunk_id": {"type": "string"},
                           "record": {"anyOf": [INVOICE_SCHEMA, {"type": "null"}]}},
            "required": ["chunk_id", "record"]}}},
        "required": ["results"],
    }
    identifiers = [str(index) for index in range(len(chunk_texts))]
    payload = json.dumps([{"chunk_id": identifier, "text": text}
                         for identifier, text in zip(identifiers, chunk_texts)], ensure_ascii=False)
    prompt = build_prompt() + "\nReturn one result for every supplied chunk_id. Preserve each ID exactly. Use record=null when extraction fails. Never combine chunks."
    parsed = _extract(schema, prompt, payload, retries)
    if not parsed:
        return [None] * len(chunk_texts)
    mapped = {}
    for result in parsed.get("results", []):
        identifier = result.get("chunk_id")
        if identifier not in identifiers or identifier in mapped:
            return [None] * len(chunk_texts)
        mapped[identifier] = result.get("record")
    return [mapped.get(identifier) for identifier in identifiers]
