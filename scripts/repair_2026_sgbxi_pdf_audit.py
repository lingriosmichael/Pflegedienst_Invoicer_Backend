#!/usr/bin/env python3
"""Evidence-backed repair of the SGB XI Jan-Jul 2026 discrepancies found by
cross-checking pdf_audit_lines against care_events (see
GET /analytics/sgbxi-leistungscodes/audit?year=2026 and TECHNICAL_HANDOVER.md §29/§30).

All 8 non-zero (month, Leistungscode) cells that endpoint reported for
Jan-Jul 2026 were traced individually, patient by patient, back to a specific
source PDF page. They resolve to exactly two root causes, each the same
pattern already seen and fixed in 2025 (a backdated Verordnung block filed
alongside the current month's submission, missing from care_events):

1. Feb 2026, patient SCHWERDTNER JOCHEN (pat_bb52149bc458): a backdated
   single-day January block (Verordnung 01.01.26, Pflegezeitraum 23.01.26,
   Pflegekonto 4010) filed in the Feb 2026 submission
   (data/abrechnung/2026/02.pdf, page 25) never made it into care_events.
   Explains both February cells.
2. Mar 2026, patient HANKE BRIGITTE (pat_0134d9735e1b): the entire March
   monthly care_event (Verordnung 01.03.26, Pflegezeitraum 01.03.26-31.03.26,
   Pflegekonto 4030, data/abrechnung/2026/03.pdf page 11) is missing, despite
   this patient having other 2026 months present.

Neither patient has a sent/paid invoice or a rendered PDF anywhere in
Jan-Jul 2026 (org-wide: 182 SGBXI events, 177 billing_details, all
billing_status="invoice_needed", zero pdf_path) — verified before writing
this script.

Idempotent: both inserts use persist_record's origin_chunk_id dedup key.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.connection import get_database
from app.import_records import persist_record
import app.database as database


def backfill_february_schwerdtner_jochen():
    db = get_database()
    existing = list(db.care_events.find({
        "org_id": "org_default", "event_type": "SGBXI",
        "patient_id": "pat_bb52149bc458", "invoicing_month": "022026",
    }))
    if any(e.get("period_start_date") == "23.01.26" for e in existing):
        print("Feb 2026 Jan-period backfill for pat_bb52149bc458 already exists — nothing to do.")
        return

    origin_chunk_id = "pdf_audit_backfill:2026/02.pdf:page25:L464862561:Jan2026period:022026"
    data = {
        "patient": {
            "name": "SCHWERDTNER, JOCHEN",
            "insurance_number": "L464862561",
            "birthdate": "29.05.1944",
            "care_level": "2",
        },
        "invoice": {
            "summe_total": "20.40",
            "summe_covered": "20.40",
            "pflegezeitraum_beginn": "23.01.26",
            "pflegezeitraum_ende": "23.01.26",
            "care_account": "4010",
        },
        "services": [
            {"code": "01010011", "description": "Beheizen des Wohnbereiches",
             "quantity": "1", "unit_price": "8.07", "total_price": "8.07"},
            {"code": "01010030", "description": "Häusliche Betreuung",
             "quantity": "1", "unit_price": "12.33", "total_price": "12.33"},
        ],
    }
    result = persist_record(data, origin_chunk_id, "022026", record_type="SGBXI", care_account="4010")
    print(f"Backfilled Feb 2026 (Jan-period) event for pat_bb52149bc458: {result}")


def backfill_march_hanke_brigitte():
    db = get_database()
    existing = list(db.care_events.find({
        "org_id": "org_default", "event_type": "SGBXI",
        "patient_id": "pat_0134d9735e1b", "invoicing_month": "032026",
    }))
    if existing:
        print("Mar 2026 SGBXI event for pat_0134d9735e1b already exists — nothing to do.")
        return

    origin_chunk_id = "pdf_audit_backfill:2026/03.pdf:page11:B609473758:032026"
    data = {
        "patient": {
            "name": "HANKE, BRIGITTE",
            "insurance_number": "B609473758",
            "birthdate": "17.02.1941",
            "care_level": "4",
        },
        "invoice": {
            "summe_total": "2136.21",
            "summe_covered": "1859.00",
            "pflegezeitraum_beginn": "01.03.26",
            "pflegezeitraum_ende": "31.03.26",
            "care_account": "4030",
        },
        "services": [
            {"code": "01010001", "description": "Kleine Morgen/Abendtoilette mit",
             "quantity": "8", "unit_price": "27.16", "total_price": "217.28"},
            {"code": "01010005", "description": "Lagern/Betten",
             "quantity": "5", "unit_price": "6.61", "total_price": "33.05"},
            {"code": "01010005", "description": "Lagern/Betten",
             "quantity": "15", "unit_price": "6.61", "total_price": "99.15"},
            {"code": "01010005", "description": "Lagern/Betten",
             "quantity": "11", "unit_price": "6.61", "total_price": "72.71"},
            {"code": "01010003", "description": "Grosse Morgen/Abendtoilette mit",
             "quantity": "31", "unit_price": "42.57", "total_price": "1319.67"},
            {"code": "0101016a", "description": "Zubereitung einer sonstigen Mahlzeit",
             "quantity": "1", "unit_price": "7.34", "total_price": "7.34"},
            {"code": "01010030", "description": "Häusliche Betreuung",
             "quantity": "15", "unit_price": "12.33", "total_price": "184.95"},
            {"code": "01010030", "description": "Häusliche Betreuung",
             "quantity": "10", "unit_price": "12.33", "total_price": "123.30"},
            {"code": "01013021", "description": "Ausbildungspauschale nach § 26 PflBG",
             "quantity": "1", "unit_price": "78.76", "total_price": "78.76"},
        ],
    }
    result = persist_record(data, origin_chunk_id, "032026", record_type="SGBXI", care_account="4030")
    print(f"Backfilled Mar 2026 event for pat_0134d9735e1b: {result}")


def refresh_billing_details():
    for month in ("022026",):
        created = database.mark_month_ready_for_generation(month, only_positive=False)
        print(f"mark_month_ready_for_generation({month}): {created} billing_details row(s) created")


if __name__ == "__main__":
    backfill_february_schwerdtner_jochen()
    # backfill_march_hanke_brigitte() intentionally skipped: a missing monthly
    # care_event for a patient is not necessarily an error — patients are not
    # billed every month by default, so this one is left alone per explicit
    # instruction rather than treated as data loss.
    refresh_billing_details()
