from openai import OpenAI
import re
import json
import os

client = OpenAI(
    api_key="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",
)

LOG_PATH = "logs/invalid_json.log"
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

def safe_parse_json(text):
    json_match = re.search(r"\[.*\]", text, re.DOTALL)
    if not json_match:
        msg = f"❌ Could not find JSON array in response:\n{text}\n{'-'*80}\n"
        print(msg)
        with open(LOG_PATH, "a") as f:
            f.write("NO JSON FOUND:\n" + msg)
        return None

    json_text = json_match.group(0).strip()

    try:
        return json.loads(json_text)
    except json.JSONDecodeError as e:
        msg = f"❌ Invalid JSON:\n{json_text}\nError: {e}\n{'-'*80}\n"
        print(msg)
        with open(LOG_PATH, "a") as f:
            f.write("JSON DECODE ERROR:\n" + msg)
        return None
def build_batch_user_message(chunks):
    return "\n\n---\n\n".join(chunks)

system_prompt = '''
                You are an expert at reading German Pflegedienst invoices.

                You will receive multiple billing blocks. Each block starts with "Verordnung:" and contains:
                - Patient info
                - Service entries (lines with quantity, code, description, unit/total prices)
                - Invoice totals

                Your task:
                - Extract one structured JSON object **per block**
                - Return a JSON **array** of all objects
                - Format monetary values in German format (e.g. "1.234,56")
                - Add `"private_rechnung": true` if the service is not covered by insurance or marked as unpaid

                Return a single list (JSON array) of objects. Do not include explanations, comments, or markdown.
                ---
                Example block:
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

                Expected object:

                {
                "patient": {
                    "name": "Müller, Karl",
                    "birthdate": "06.01.41",
                    "insurance_number": "L011897478",
                    "care_level": "4",
                    "pflege_konto": "4030"
                },
                "services": [
                    {
                    "quantity": "31",
                    "code": "0101002a",
                    "description": "Kleine Morgen/Abendtoilette mit",
                    "unit_price": "15,41",
                    "total_price": "477,71"
                    },
                    {
                    "quantity": "31",
                    "code": "01010003",
                    "description": "Grosse Morgen/Abendtoilette mit",
                    "unit_price": "42,57",
                    "total_price": "1.319,67"
                    },
                    {
                    "quantity": "1",
                    "code": "01013021",
                    "description": "Ausbildungspauschale nach § 26 PflBG",
                    "unit_price": "61,23",
                    "total_price": "61,23"
                    }
                ],
                "invoice": {
                    "pflegezeitraum_beginn": "01.03.25",
                    "pflegezeitraum_ende": "31.03.25",
                    "summe_covered": "1.758,61",
                    "summe_total": "1.858,61"
                },
                "private_rechnung": false
                }

                ---
                Return an array of such objects for the blocks you receive.
'''

def extract_structured_data_batch(chunks, retries=1):
    for attempt in range(retries + 1):
        try:
            response = client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": system_prompt.strip()},
                    {"role": "user", "content": build_batch_user_message(chunks)}
                ],
                max_tokens=4000,
            )
            content = response.choices[0].message.content
            parsed = safe_parse_json(content)
            if parsed:
                return parsed
        except Exception as e:
            print(f"❌ OpenAI API error: {e}")
        print(f"🔁 Retry {attempt + 1} failed")

    return None
