#!/usr/bin/env python3
"""Evidence-backed repair of every SGB XI 2025 discrepancy found by
cross-checking pdf_audit_lines against care_events (see
GET /analytics/sgbxi-leistungscodes/audit?year=2025 and TECHNICAL_HANDOVER.md §29/§30).

All 15 non-zero (month, Leistungscode) cells that endpoint reported for 2025
were traced individually, patient by patient, back to a specific source PDF
page. They resolve to exactly four root causes, each fixed below:

1. Dec 2025, patient HENNIG REGINA (evt_a395dd99f042): 5 of 13 service lines
   had quantities that did not match the PDF and were identical to a later
   Jan 2026 event for the same patient — a prepared-chunk reuse across two
   different-month imports. Explains all 5 December cells.
2. Sep 2025, patient WALTER URSULA: an entire monthly care_event was missing
   (present for every neighboring month). Explains both September cells.
3. Jan 2025, patient MEISS BERND: of three separate Verordnung blocks billed
   in the same January submission (two backdated to Nov/Dec 2024, one for the
   actual Jan 2025 period), only two made it into care_events — the Nov 2024
   block is missing entirely. Explains all 4 January cells.
4. Jul 2025, patient LIPOWSKI MAIK: a backdated June-2025 Verordnung block
   (Pflegekonto 4040) filed in the July submission never made it into
   care_events. Explains all 4 July cells.

Each correction/insert was verified line-by-line against its source PDF page
before being written here; this script exists so the exact change is
reviewable and re-runnable rather than a one-shot interactive edit with no
record.

None of the four affected care_events had a sent/paid invoice or a rendered
PDF at the time of this repair (billing_status="invoice_needed", pdf_path is
None throughout 2025 org-wide) — verified separately before writing this
script. After applying, run `mark_ready` for the affected invoicing_month(s)
again through the normal endpoint/UI so billing_details gets computed by the
real formula rather than by this script (done automatically below via
refresh_billing_details()).

Idempotent: every insert uses persist_record's origin_chunk_id dedup key, and
the December correction is safe to re-run (it just re-sets the same target
values and exits early once they already match).
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.connection import get_database
from app.import_records import persist_record
from app.utils.parsing import generate_id
from app.utils.validation import money
import app.database as database


def repair_december_hennig_regina():
    """evt_a395dd99f042 (patient pat_d2620a6e7241, invoicing_month=122025):
    5 of 13 service lines had quantities that did not match the source PDF
    (rzh 12.2025.pdf / 2025/11.pdf, page 14, Verordnung 01.12.25-31.12.25 for
    HENNIG, REGINA - C565592395) and were identical to a later Jan 2026
    care_event for the same patient — consistent with a prepared-chunk reuse
    across two different-month imports. Corrected quantities/line_totals are
    taken directly from that PDF page; the corrected sum_total (2226.25)
    matches the PDF's own printed block subtotal exactly."""
    db = get_database()
    event = db.care_events.find_one({"org_id": "org_default", "care_event_id": "evt_a395dd99f042"})
    if not event:
        print("evt_a395dd99f042 not found — already repaired or removed?")
        return

    corrections = {
        "01010003": (4.0, 170.28),
        "01010012": (14.0, 133.56),
        "01010013": (1.0, 27.89),
        "01010008": (14.0, 164.36),
        "01010030": (14.0, 172.62),
    }
    before = {s["service_code"]: (s["quantity_value"], s["line_total"]) for s in event["services"]}

    changed = []
    for service in event["services"]:
        code = service["service_code"]
        if code in corrections:
            new_qty, new_total = corrections[code]
            if service["quantity_value"] != new_qty or service["line_total"] != new_total:
                changed.append({
                    "service_code": code,
                    "quantity_before": service["quantity_value"], "quantity_after": new_qty,
                    "line_total_before": service["line_total"], "line_total_after": new_total,
                })
            service["quantity_value"] = new_qty
            service["line_total"] = new_total

    if not changed:
        print("evt_a395dd99f042 already matches the corrected values — nothing to do.")
        return

    new_sum_total = money(sum(s["line_total"] for s in event["services"]))
    now = datetime.now(timezone.utc)

    db.care_events.update_one(
        {"_id": event["_id"]},
        {"$set": {"services": event["services"], "sum_total": new_sum_total, "updated_at": str(now)[:19]}},
    )

    deleted = db.billing_details.delete_one({"org_id": "org_default", "care_event_id": "evt_a395dd99f042"})

    db.care_event_history.insert_one({
        "org_id": "org_default",
        "history_id": generate_id("hist"),
        "care_event_id": "evt_a395dd99f042",
        "action": "pdf_audit_correction",
        "corrections": changed,
        "sum_total_before": event.get("sum_total"),
        "sum_total_after": new_sum_total,
        "billing_details_reset": deleted.deleted_count > 0,
        "source": "scripts/repair_2025_sgbxi_pdf_audit.py, verified against data/abrechnung/2025/11.pdf page 14",
        "created_at": now,
    })

    print(f"Corrected evt_a395dd99f042: {len(changed)} service line(s), "
          f"sum_total {event.get('sum_total')} -> {new_sum_total}, "
          f"billing_details reset={deleted.deleted_count > 0}")


def backfill_september_walter_ursula():
    """No SGBXI care_event existed for patient pat_5dc152d1bfdb (WALTER, URSULA -
    C550372163) for invoicing_month=092025, despite one existing for every
    neighboring month — confirmed against data/abrechnung/2025/09.pdf page 16,
    Verordnung 01.09.25-21.09.25 (Pflegekonto 4020, Summe 924,42/924,42)."""
    db = get_database()
    existing = db.care_events.find_one({
        "org_id": "org_default", "event_type": "SGBXI",
        "patient_id": "pat_5dc152d1bfdb", "invoicing_month": "092025",
    })
    if existing:
        print("Sep 2025 SGBXI event for pat_5dc152d1bfdb already exists — nothing to do.")
        return

    origin_chunk_id = "pdf_audit_backfill:2025/09.pdf:page16:C550372163:092025"
    data = {
        "patient": {
            "name": "WALTER, URSULA",
            "insurance_number": "C550372163",
            "birthdate": "17.06.1926",
            "care_level": "3",
        },
        "invoice": {
            "summe_total": "924.42",
            "summe_covered": "924.42",
            "pflegezeitraum_beginn": "01.09.25",
            "pflegezeitraum_ende": "21.09.25",
            "care_account": "4020",
        },
        "services": [
            {"code": "01010003", "description": "Grosse Morgen/Abendtoilette mit",
             "quantity": "21", "unit_price": "42.57", "total_price": "893.97"},
            {"code": "01013021", "description": "Ausbildungspauschale nach § 26 PflBG",
             "quantity": "1", "unit_price": "30.45", "total_price": "30.45"},
        ],
    }
    result = persist_record(data, origin_chunk_id, "092025", record_type="SGBXI", care_account="4020")
    print(f"Backfilled Sep 2025 event for pat_5dc152d1bfdb: {result}")


def backfill_january_meiss_bernd():
    """Patient pat_75131c7ceb61 (MEISS, BERND - E813094835) had three separate
    Verordnung blocks in the January 2025 submission (all filed under
    invoicing_month=012025): a Dec-2024-period block and the actual Jan-2025
    block both exist in care_events (evt_1a416ea208a4, evt_2c3b09f5c70e); the
    third — Verordnung 01.11.24, Pflegezeitraum 30.11.24, Pflegekonto 4010,
    data/abrechnung/2025/01.pdf page 19 — is missing entirely."""
    db = get_database()
    existing = list(db.care_events.find({
        "org_id": "org_default", "event_type": "SGBXI",
        "patient_id": "pat_75131c7ceb61", "invoicing_month": "012025",
    }))
    if any(e.get("period_start_date") == "30.11.24" for e in existing):
        print("Jan 2025 Nov-period backfill for pat_75131c7ceb61 already exists — nothing to do.")
        return

    origin_chunk_id = "pdf_audit_backfill:2025/01.pdf:page19:E813094835:Nov2024period:012025"
    data = {
        "patient": {
            "name": "MEISS, BERND",
            "insurance_number": "E813094835",
            "birthdate": "23.09.1955",
            "care_level": "3",  # current patient_profiles value; PDF's Nov-2024 snapshot predates a later Pflegegrad change
        },
        "invoice": {
            "summe_total": "66.82",
            "summe_covered": "66.82",
            "pflegezeitraum_beginn": "30.11.24",
            "pflegezeitraum_ende": "30.11.24",
            "care_account": "4010",
        },
        "services": [
            {"code": "01010003", "description": "Grosse Morgen/Abendtoilette mit",
             "quantity": "1", "unit_price": "42.57", "total_price": "42.57"},
            {"code": "01010012", "description": "Reinigen der Wohnung",
             "quantity": "1", "unit_price": "9.54", "total_price": "9.54"},
            {"code": "01010030", "description": "Häusliche Betreuung",
             "quantity": "1", "unit_price": "12.33", "total_price": "12.33"},
            {"code": "01013021", "description": "Ausbildungspauschale nach § 26 PflBG",
             "quantity": "1", "unit_price": "2.38", "total_price": "2.38"},
        ],
    }
    result = persist_record(data, origin_chunk_id, "012025", record_type="SGBXI", care_account="4010")
    print(f"Backfilled Jan 2025 (Nov-period) event for pat_75131c7ceb61: {result}")


def backfill_july_lipowski_maik():
    """Patient pat_43a16a9c9f20 (LIPOWSKI, MAIK - Q265731778) has a backdated
    June-2025 Verordnung block (Pflegekonto 4040, single-day Pflegezeitraum
    02.06.25) filed in the July 2025 submission — data/abrechnung/2025/07.pdf
    page 20 — that never made it into care_events."""
    db = get_database()
    existing = list(db.care_events.find({
        "org_id": "org_default", "event_type": "SGBXI",
        "patient_id": "pat_43a16a9c9f20", "invoicing_month": "072025",
    }))
    if any(e.get("period_start_date") == "02.06.25" for e in existing):
        print("Jul 2025 June-period backfill for pat_43a16a9c9f20 already exists — nothing to do.")
        return

    origin_chunk_id = "pdf_audit_backfill:2025/07.pdf:page20:Q265731778:Jun2025period:072025"
    data = {
        "patient": {
            "name": "LIPOWSKI, MAIK",
            "insurance_number": "Q265731778",
            "birthdate": "09.03.1984",
            "care_level": "5",
        },
        "invoice": {
            "summe_total": "60.72",
            "summe_covered": "60.72",
            "pflegezeitraum_beginn": "02.06.25",
            "pflegezeitraum_ende": "02.06.25",
            "care_account": "4040",
        },
        "services": [
            {"code": "01010003", "description": "Grosse Morgen/Abendtoilette mit",
             "quantity": "1", "unit_price": "42.57", "total_price": "42.57"},
            {"code": "01010005", "description": "Lagern/Betten",
             "quantity": "1", "unit_price": "6.61", "total_price": "6.61"},
            {"code": "01010012", "description": "Reinigen der Wohnung",
             "quantity": "1", "unit_price": "9.54", "total_price": "9.54"},
            {"code": "01013021", "description": "Ausbildungspauschale nach § 26 PflBG",
             "quantity": "1", "unit_price": "2.00", "total_price": "2.00"},
        ],
    }
    result = persist_record(data, origin_chunk_id, "072025", record_type="SGBXI", care_account="4040")
    print(f"Backfilled Jul 2025 (Jun-period) event for pat_43a16a9c9f20: {result}")


def refresh_billing_details():
    for month in ("012025", "072025", "092025", "122025"):
        created = database.mark_month_ready_for_generation(month, only_positive=False)
        print(f"mark_month_ready_for_generation({month}): {created} billing_details row(s) created")


if __name__ == "__main__":
    repair_december_hennig_regina()
    backfill_september_walter_ursula()
    backfill_january_meiss_bernd()
    backfill_july_lipowski_maik()
    refresh_billing_details()
