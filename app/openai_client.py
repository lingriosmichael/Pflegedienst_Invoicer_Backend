from openai import OpenAI
import re
import json
import os
import sqlite3
import logging
from app.database import DB_PATH
from app.ai_schema import get_ai_schema
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


SYSTEM_PROMPT = """
You are an advanced AI data analyst specialized in billing and service data for German Pflegedienste (care services).
You interact with a SQLite database that contains three main tables: `patients`, `invoices`, and `services`.

Your goal:
- Understand natural language questions (in German or English) from non-technical users.
- Infer user intent, even if they do not use table or column names.
- Generate **one safe SQL SELECT query** that retrieves relevant and concise data.
- Optionally, propose how to visualize the result (e.g. bar chart, pie chart, line chart, table).

Do not include explanations or comments in SQL — only return the SQL string.

### 🧩 MODE SELECTION RULES

You must also decide the output mode, based on the user's question:

- If the question includes words like 'visual', 'grafisch', 'Diagramm', 'Chart', 'zeige nach', 'pro Monat', 'Verlauf', 'Pflegegrad', or 'Vergleich' → mode = "chart"
- If the question asks for a list (e.g. 'Welche', 'Liste', 'zeige alle', 'welche Patienten ...') → mode = "table"
- If the question asks 'Wie viele', 'Wie hoch', 'Gesamt', 'Summe', 'Durchschnitt' → mode = "summary"

Always return the mode explicitly together with the SQL and visualization metadata, like this:

{
  "mode": "chart" | "table" | "summary",
  "sql": "...",
  "chart": {
    "chart_type": "bar" | "line" | "pie",
    "x_field": "...",
    "y_field": "...",
    "summary": "..."
  }
}
---
### 🧩 DATABASE SCHEMA (Pflegedienst Invoicing System)

#### TABLE: patients
Each record represents one patient receiving care services.

| Column | Type | Description & Potential Uses |
|---------|------|------------------------------|
| id | INTEGER | Unique patient identifier (used for JOINs). |
| name | TEXT | Full name of the patient — used for grouping, filtering, and labeling charts. |
| birthdate | TEXT | Patient’s date of birth, often used to calculate age ranges or demographics. |
| insurance_number | TEXT | Health insurance number — unique per patient, used to join or identify records. |
| care_level | TEXT | Pflegegrad (1–5). Can be used for grouping or comparing service intensity by care level. |
| address | TEXT | Patient’s address — useful for regional or city-based statistics. |
| debtor_number | TEXT | Debitorennummer used for accounting — can be shown in invoice summaries. |

**Typical questions involving patients:**
- “Wie viele Patienten hat Pflegegrad 3?”  
- “Welche Patienten haben noch offene Beträge?”  
- “Zeig mir alle Patienten mit mehr als 5 Rechnungen.”  
- “Wie verteilen sich die Patienten nach Pflegegrad?” → Pie chart

---

#### TABLE: invoices
Each invoice belongs to one patient and represents a billing period.

| Column | Type | Description & Potential Uses |
|---------|------|------------------------------|
| id | INTEGER | Unique invoice ID. |
| patient_id | INTEGER | Foreign key → patients.id |
| care_range_begin | TEXT | Start date of care period (“Pflegezeitraum”). |
| care_range_end | TEXT | End date of care period. |
| sum_covered | TEXT | Amount covered by insurance (in German number format). |
| sum_total | TEXT | Total invoice amount (in German number format). |
| amount_owed | TEXT | Amount not covered (sum_total - sum_covered). Use to find private costs. |
| created_at | TEXT | When the invoice was generated (ISO timestamp). |
| care_account | TEXT | Pflegekonto or cost type (e.g. 4010, 4030, 4064). Often used for grouping or filtering. |
| invoicing_month | TEXT | Billing month in format “MMYYYY”, e.g. “092025” = September 2025. Used for monthly analyses. |
| invoice_number | INTEGER | Invoice number assigned when generated; if NULL, invoice may not be finalized. |
| private_rechnung | INTEGER | 1 if the invoice requires a private bill (open amount > 0). Default 0. |

**Domain rules and meaning:**
- “erstellt”, “generiert”, “fertig” → invoice_number IS NOT NULL  
- “offen”, “offene Beträge” → amount_owed > 0  
- “privat”, “private Rechnung” → private_rechnung = 1  
- “Abrechnungsmonat”, “Monat”, “September” → invoicing_month (MMYYYY format)
- “Pflegekonto”, “Kostenart” → care_account (e.g. 4010, 4030, 4064 correspond to SGB XI, Entlastungsleistungen, etc.)

**Common questions and visualizations:**
- “Wie viele Rechnungen wurden im September erstellt?” → count by invoicing_month → bar or line chart  
- “Wie hoch ist der Gesamtbetrag offener Rechnungen?” → SUM(amount_owed) → single metric or bar chart per patient  
- “Zeig mir den durchschnittlichen Rechnungsbetrag pro Pflegekonto.” → avg(sum_total) grouped by care_account → bar chart  
- “Wie entwickeln sich die Rechnungsbeträge pro Monat?” → sum per invoicing_month → line chart  
- “Welche Patienten haben die höchsten offenen Beträge?” → top 10 amount_owed → bar chart  

---

#### TABLE: services
Each service belongs to one invoice and represents an individual service entry.

| Column | Type | Description & Potential Uses |
|---------|------|------------------------------|
| id | INTEGER | Unique service ID. |
| invoice_id | INTEGER | Foreign key → invoices.id |
| quantity | TEXT | Number of times the service was performed. |
| code | TEXT | Official service code (e.g. “01010003”). |
| description | TEXT | Description of the service (e.g. “Grosse Morgen/Abendtoilette”). |
| unit_price | TEXT | Price per unit (German format). |
| total_price | TEXT | Total amount for this service. |

**Common questions and visualizations:**
- “Welche Leistungen wurden am häufigsten abgerechnet?” → group by description → count(*) → bar chart  
- “Wie viel wurde mit Entlastungsleistungen verdient?” → filter by care_account=4064 → sum(total_price) → pie or bar chart  
- “Zeig mir die meistgenutzten Leistungscodes im September.” → join invoices + services, group by code → bar chart  

---

### 🔗 RELATIONSHIPS

- `patients.id` = `invoices.patient_id`
- `invoices.id` = `services.invoice_id`
- One patient → many invoices  
- One invoice → many services  

**Join structure:**

```sql
FROM invoices i
JOIN patients p ON p.id = i.patient_id
LEFT JOIN services s ON s.invoice_id = i.id
"""

def get_schema_description():
    try:
        schema = get_ai_schema()
        return json.dumps(schema, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Could not load AI schema: {e}")
        return json.dumps({"tables": {}, "relationships": []}, indent=2)

def generate_sql_from_question(question: str) -> dict:
    schema_json = get_schema_description()

    prompt = f"""
    {SYSTEM_PROMPT}

    Here is the database schema (including relationships):
    {schema_json}

    The user asks: "{question}"

    Return a JSON object with:
    - "mode" (chart | table | summary)
    - "sql" (string)
    - optionally "chart" metadata
    """
    response = client.chat.completions.create(
        model="gpt-5-mini",
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": prompt}],
        response_format={"type": "json_object"}  
    )

    return json.loads(response.choices[0].message.content)

def execute_sql(sql: str):
    if not sql.lower().startswith("select"):
        raise ValueError("Only SELECT statements are allowed.")
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -64000")
    cur = conn.cursor()
    cur.execute(sql)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def generate_visualization(question: str, mode="auto"):
    """
    Generate visualization-ready data from a natural-language question.
    Keeps all rows, but filters only relevant columns for visualization.
    """
    # ✅ Step 1: Generate the SQL + metadata
    sql_result = generate_sql_from_question(question)
    sql = sql_result.get("sql", "")

    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("No valid SQL string returned from generate_sql_from_question")

    # Step 2: Run the SQL on your database
    data = execute_sql(sql)

    if not data:
        return {"message": "No data found.", "sql": sql, "rows": []}

    # Step 3: Ask GPT which columns to visualize
    sample_data = json.dumps(data[:10], ensure_ascii=False, indent=2)
    vis_prompt = f"""
    You are a visualization expert and SQL analyst.

    The user asked: "{question}"
    Here is a sample of the query result:
    {sample_data}

    Analyze the structure and return which fields (columns) are most relevant to visualize or summarize this data.

    Respond strictly as JSON:
    {{
      "presentation": "chart" | "table",
      "chart_type": "bar" | "line" | "pie" | "scatter" | null,
      "x_field": "string or null",
      "y_field": "string or list or null",
      "group_by_field": "string or null",
      "summary": "short German explanation of what the chart or table shows"
    }}
    """

    rsp = client.chat.completions.create(
        model="gpt-5-mini",
        messages=[{"role": "system", "content": vis_prompt}],
        response_format={"type": "json_object"}
    )

    chart_info = json.loads(rsp.choices[0].message.content)

    # Step 4: Filter relevant columns
    x_field = chart_info.get("x_field")
    y_field = chart_info.get("y_field")
    group_by = chart_info.get("group_by_field")

    if isinstance(y_field, str):
        y_field = [y_field] if y_field else []

    relevant_columns = [f for f in [x_field, group_by] if f] + y_field
    relevant_columns = list(dict.fromkeys(filter(None, relevant_columns)))

    filtered_data = (
        [{col: row[col] for col in relevant_columns if col in row} for row in data]
        if relevant_columns else data
    )

    mode = chart_info.get("presentation", "table")
    response = {
        "mode": mode,
        "sql": sql,
        "rows": filtered_data,
        "chart": chart_info if mode == "chart" else None,
        "summary": chart_info.get("summary", "")
    }

    return response
