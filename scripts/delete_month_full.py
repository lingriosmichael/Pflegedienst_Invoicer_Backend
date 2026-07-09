"""
Delete all data related to a month from MongoDB, including leftover records
that still appear in analytics because their service period falls in that month.

Usage:
    python scripts/delete_month_full.py 052026 --dry-run
    python scripts/delete_month_full.py 052026
    python scripts/delete_month_full.py 052026 --yes

This script removes:
    - care_events matched by invoicing_month
    - care_events whose period_start_date or period_end_date falls in the month
    - billing_details matched directly by invoicing_month
    - billing_details linked to matched care_event_ids
    - care_event_history linked to matched care_event_ids
    - care_event_history rows with abrechnungsmonat == month
    - billing_summary rows matched by abrechnungsmonat or billing_month

It is broader than scripts/delete_month.py and is intended for "clean slate"
re-imports when analytics still show leftover records for a month.
"""

import argparse
import os
import re
import sys
from typing import Dict, List

# Allow running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.mongodb_config import get_database


def validate_month(month: str) -> str:
    if not re.fullmatch(r"(0[1-9]|1[0-2])\d{4}", month):
        raise ValueError("Month must be in MMYYYY format, e.g. 052026")
    return month


def build_period_query(month: str) -> Dict:
    mm = month[:2]
    yyyy = month[2:]
    yy = yyyy[-2:]

    short_pattern = rf"^\d{{2}}\.{re.escape(mm)}\.{re.escape(yy)}$"
    long_pattern = rf"^\d{{2}}\.{re.escape(mm)}\.{re.escape(yyyy)}$"

    return {
        "$or": [
            {"period_start_date": {"$regex": short_pattern}},
            {"period_start_date": {"$regex": long_pattern}},
            {"period_end_date": {"$regex": short_pattern}},
            {"period_end_date": {"$regex": long_pattern}},
        ]
    }


def collect_targets(month: str) -> Dict:
    db = get_database()

    period_query = build_period_query(month)

    care_event_query = {
        "$or": [
            {"invoicing_month": month},
            period_query,
        ]
    }

    care_events = list(
        db.care_events.find(
            care_event_query,
            {
                "_id": 0,
                "care_event_id": 1,
                "event_type": 1,
                "invoicing_month": 1,
                "period_start_date": 1,
                "period_end_date": 1,
            },
        )
    )
    care_event_ids = [doc["care_event_id"] for doc in care_events if doc.get("care_event_id")]

    billing_details = list(
        db.billing_details.find(
            {
                "$or": [
                    {"invoicing_month": month},
                    {"care_event_id": {"$in": care_event_ids}},
                ]
            },
            {"_id": 1, "billing_detail_id": 1, "care_event_id": 1, "invoicing_month": 1},
        )
    )

    history_by_event = db.care_event_history.count_documents(
        {"care_event_id": {"$in": care_event_ids}}
    )
    history_by_month = db.care_event_history.count_documents({"abrechnungsmonat": month})

    billing_summary = db.billing_summary.count_documents(
        {"$or": [{"abrechnungsmonat": month}, {"billing_month": month}]}
    )

    by_event_type: Dict[str, int] = {}
    for doc in care_events:
        event_type = doc.get("event_type") or "unknown"
        by_event_type[event_type] = by_event_type.get(event_type, 0) + 1

    invoicing_months: Dict[str, int] = {}
    for doc in care_events:
        key = str(doc.get("invoicing_month"))
        invoicing_months[key] = invoicing_months.get(key, 0) + 1

    return {
        "care_event_query": care_event_query,
        "care_event_ids": care_event_ids,
        "care_events": care_events,
        "billing_details": billing_details,
        "counts": {
            "care_events": len(care_events),
            "billing_details": len(billing_details),
            "care_event_history_by_event": history_by_event,
            "care_event_history_by_month": history_by_month,
            "billing_summary": billing_summary,
        },
        "breakdown": {
            "care_events_by_event_type": by_event_type,
            "care_events_by_invoicing_month": invoicing_months,
        },
    }


def print_summary(month: str, targets: Dict) -> None:
    counts = targets["counts"]
    breakdown = targets["breakdown"]

    print(f"\nMonth: {month}")
    print(f"  care_events:                 {counts['care_events']}")
    print(f"  billing_details:             {counts['billing_details']}")
    print(f"  care_event_history (events): {counts['care_event_history_by_event']}")
    print(f"  care_event_history (month):  {counts['care_event_history_by_month']}")
    print(f"  billing_summary:             {counts['billing_summary']}")

    print("\nCare events by event_type:")
    if breakdown["care_events_by_event_type"]:
        for event_type, count in sorted(breakdown["care_events_by_event_type"].items()):
            print(f"  {event_type}: {count}")
    else:
        print("  none")

    print("\nCare events by invoicing_month:")
    if breakdown["care_events_by_invoicing_month"]:
        for invoicing_month, count in sorted(breakdown["care_events_by_invoicing_month"].items()):
            print(f"  {invoicing_month}: {count}")
    else:
        print("  none")


def delete_targets(month: str, assume_yes: bool = False, dry_run: bool = False) -> None:
    db = get_database()
    targets = collect_targets(month)

    print_summary(month, targets)

    if dry_run:
        print("\n[DRY RUN] No data deleted.")
        return

    if not assume_yes:
        confirm = input(
            f"\nDelete all data related to month '{month}' including service-period leftovers? (yes/no): "
        ).strip().lower()
        if confirm != "yes":
            print("Aborted.")
            return

    care_event_ids: List[str] = targets["care_event_ids"]

    care_events_deleted = db.care_events.delete_many(targets["care_event_query"]).deleted_count
    billing_details_deleted = db.billing_details.delete_many(
        {
            "$or": [
                {"invoicing_month": month},
                {"care_event_id": {"$in": care_event_ids}},
            ]
        }
    ).deleted_count
    history_deleted = db.care_event_history.delete_many(
        {
            "$or": [
                {"care_event_id": {"$in": care_event_ids}},
                {"abrechnungsmonat": month},
            ]
        }
    ).deleted_count
    billing_summary_deleted = db.billing_summary.delete_many(
        {"$or": [{"abrechnungsmonat": month}, {"billing_month": month}]}
    ).deleted_count

    print("\nDeleted:")
    print(f"  care_events:          {care_events_deleted}")
    print(f"  billing_details:      {billing_details_deleted}")
    print(f"  care_event_history:   {history_deleted}")
    print(f"  billing_summary:      {billing_summary_deleted}")

    remaining = collect_targets(month)["counts"]
    print("\nRemaining after delete:")
    print(f"  care_events:          {remaining['care_events']}")
    print(f"  billing_details:      {remaining['billing_details']}")
    print(f"  care_event_history:   {remaining['care_event_history_by_event'] + remaining['care_event_history_by_month']}")
    print(f"  billing_summary:      {remaining['billing_summary']}")

    print("\nDone. You can now re-import from scratch.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete all MongoDB data related to a month, including service-period leftovers."
    )
    parser.add_argument("month", help="Month in MMYYYY format, e.g. 052026")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without deleting")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    try:
        month = validate_month(args.month)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1

    delete_targets(month, assume_yes=args.yes, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
