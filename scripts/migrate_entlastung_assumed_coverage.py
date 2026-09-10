"""Normalize unissued Entlastungsleistung rows to the RZH-first policy.

Run without --apply to see the number of affected rows. The script never
changes issued invoices or RZH-confirmed settlements and deliberately prints
counts only, not patient data.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import DEFAULT_ORG_ID
from app.db.mongodb_config import get_database
from app.invoice_eligibility import CONFIRMED_ENTLASTUNG_STATUSES


def run(*, apply: bool) -> dict:
    database = get_database()
    event_ids = [
        event["care_event_id"]
        for event in database.care_events.find(
            {"org_id": DEFAULT_ORG_ID, "event_type": "Entleistung"},
            {"care_event_id": 1},
        )
    ]
    candidates = list(database.billing_details.find({
        "org_id": DEFAULT_ORG_ID,
        "care_event_id": {"$in": event_ids},
        "invoice_number": None,
        "billing_status": {"$nin": ["sent", "paid"]},
        "reconciliation_status": {"$nin": list(CONFIRMED_ENTLASTUNG_STATUSES)},
    }))
    result = {"dry_run": not apply, "eligible_unissued_rows": len(candidates), "updated": 0}
    if not apply:
        return result

    now = datetime.now(timezone.utc)
    for bill in candidates:
        total = round(float(bill.get("sum_total", 0) or 0), 2)
        database.billing_details.update_one({"_id": bill["_id"]}, {"$set": {
            "sum_covered": total,
            "amount_owed": 0.0,
            "billing_status": "covered_insurance",
            "reconciliation_status": "assumed_covered_until_rzh",
            "coverage_source": "assumed_full_until_rzh",
            "prior_internal_sum_covered": bill.get("sum_covered"),
            "prior_internal_amount_owed": bill.get("amount_owed"),
            "prior_internal_billing_status": bill.get("billing_status"),
            "policy_migrated_at": now,
            "updated_at": now,
        }})
        result["updated"] += 1
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="persist the policy normalization")
    options = parser.parse_args()
    print(json.dumps(run(apply=options.apply), indent=2, default=str))
