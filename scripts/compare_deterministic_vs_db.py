#!/usr/bin/env python3
"""
One-off comparison: deterministic parser output vs. what is already committed
in the database for a given billing month.

Does not write anything. Read-only against MongoDB, no OpenAI calls.

Usage (inside the backend container, where MONGODB_HOST resolves):
    docker exec pflegedienst_backend python3 scripts/compare_deterministic_vs_db.py \
        data/abrechnung/2026/07.pdf 072026
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.deterministic_parser import parse_pdf
from app.utils.parsing import GermanDecimalParser
from app.db.mongodb_config import get_database

CARE_ACCOUNT_TO_EVENT_TYPE = {
    "4062": "Consultation", "4064": "Entleistung", "4050": "Verhinderungspflege",
    "4092": "SGBV", "4010": "SGBXI", "4020": "SGBXI", "4030": "SGBXI", "4040": "SGBXI",
}


def main():
    pdf_path = sys.argv[1] if len(sys.argv) > 1 else "data/abrechnung/2026/07.pdf"
    month = sys.argv[2] if len(sys.argv) > 2 else "072026"

    db = get_database()
    events = list(db.care_events.find({"invoicing_month": month}))
    profiles = {p["patient_id"]: p for p in db.patient_profiles.find({})}
    print(f"DB: {len(events)} care_events for invoicing_month={month}")

    # Index DB events by (care_account, period_start, period_end, insurance_number)
    db_index = {}
    for event in events:
        profile = profiles.get(event["patient_id"], {})
        key = (event["care_account"], event["period_start_date"], event["period_end_date"],
               profile.get("insurance_number", ""))
        db_index.setdefault(key, []).append(event)

    results = parse_pdf(pdf_path)
    parsed = [r for r in results if r["status"] == "ok"]
    parse_failed = [r for r in results if r["status"] == "failed"]

    matched, amount_mismatch, not_in_db = [], [], []

    for r in parsed:
        record = r["record"]
        patient, invoice = record["patient"], record["invoice"]
        care_account = patient["pflege_konto"]
        key = (care_account, invoice["pflegezeitraum_beginn"], invoice["pflegezeitraum_ende"],
               patient["insurance_number"])
        candidates = db_index.get(key, [])
        if not candidates:
            not_in_db.append(r)
            continue
        db_event = candidates[0]
        parsed_total = GermanDecimalParser.parse(invoice["summe_total"])
        parsed_covered = GermanDecimalParser.parse(invoice["summe_covered"])
        if abs(parsed_total - db_event["sum_total"]) > 0.01 or abs(parsed_covered - db_event["sum_covered"]) > 0.01:
            amount_mismatch.append({
                "chunk": r["index"], "patient": patient["name"],
                "parsed_total": parsed_total, "db_total": db_event["sum_total"],
                "parsed_covered": parsed_covered, "db_covered": db_event["sum_covered"],
            })
        else:
            matched.append(r)

    print(f"\nSource PDF chunks: {len(results)}  parsed: {len(parsed)}  parse_failed: {len(parse_failed)}")
    print(f"Matched DB events with identical totals: {len(matched)}")
    print(f"Matched DB events but amount mismatch: {len(amount_mismatch)}")
    print(f"Parsed chunks with no corresponding DB event "
          f"(likely a care_account/mode never imported for this month, e.g. SGBV/Verhinderungspflege): "
          f"{len(not_in_db)}")

    if parse_failed:
        print("\n--- parse failures ---")
        for r in parse_failed:
            print(f"  chunk {r['index']}: {r['error']}")

    if amount_mismatch:
        print("\n--- amount mismatches (needs investigation) ---")
        for m in amount_mismatch:
            print(f"  chunk {m['chunk']} ({m['patient']}): "
                  f"parsed total={m['parsed_total']} vs db={m['db_total']}, "
                  f"parsed covered={m['parsed_covered']} vs db={m['db_covered']}")

    if not_in_db:
        from collections import Counter
        accounts = Counter(r["record"]["patient"]["pflege_konto"] for r in not_in_db)
        print(f"\n--- chunks with no DB match, by care_account ---")
        print(dict(accounts))

    # Coverage check the other direction: did every DB event for this month
    # find a matching parsed chunk?
    parsed_keys = set()
    for r in parsed:
        patient, invoice = r["record"]["patient"], r["record"]["invoice"]
        parsed_keys.add((patient["pflege_konto"], invoice["pflegezeitraum_beginn"],
                          invoice["pflegezeitraum_ende"], patient["insurance_number"]))
    db_only = []
    for key, evs in db_index.items():
        if key not in parsed_keys:
            db_only.extend(evs)
    print(f"\nDB events with no corresponding parsed chunk in this PDF: {len(db_only)}")
    if db_only:
        for e in db_only[:20]:
            print(f"  {e['care_event_id']} account={e['care_account']} "
                  f"{e['period_start_date']}-{e['period_end_date']} total={e['sum_total']}")


if __name__ == "__main__":
    main()
