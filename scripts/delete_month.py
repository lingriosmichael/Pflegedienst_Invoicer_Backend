"""
Delete all data for a given abrechnungsmonat from MongoDB.

Usage:
    python scripts/delete_month.py 032026

Collections affected:
    - care_events          (invoicing_month field)
    - care_event_history   (care_event_id references + file_import records)
    - billing_summary      (abrechnungsmonat field)
"""

import sys
import os

# Allow running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.mongodb_config import get_database


def delete_month(month: str, dry_run: bool = False):
    db = get_database()

    # 1. Find all care_event_ids for this month
    care_events = list(db.care_events.find(
        {"invoicing_month": month},
        {"care_event_id": 1}
    ))
    care_event_ids = [doc["care_event_id"] for doc in care_events]

    print(f"\nMonth: {month}")
    print(f"  care_events to delete:        {len(care_event_ids)}")

    # 2. Count history records linked to those care_event_ids
    history_by_event = db.care_event_history.count_documents(
        {"care_event_id": {"$in": care_event_ids}}
    )
    # 3. Count file_import history records for this month
    history_by_month = db.care_event_history.count_documents(
        {"abrechnungsmonat": month, "action": "file_import"}
    )
    print(f"  care_event_history (events):  {history_by_event}")
    print(f"  care_event_history (imports): {history_by_month}")

    # 4. Count billing summary
    billing = db.billing_summary.count_documents({"abrechnungsmonat": month})
    print(f"  billing_summary:              {billing}")

    if dry_run:
        print("\n[DRY RUN] No data deleted.")
        return

    confirm = input(f"\nDelete all of the above for month '{month}'? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        return

    # Delete
    r1 = db.care_events.delete_many({"invoicing_month": month})
    r2 = db.care_event_history.delete_many({"care_event_id": {"$in": care_event_ids}})
    r3 = db.care_event_history.delete_many({"abrechnungsmonat": month, "action": "file_import"})
    r4 = db.billing_summary.delete_many({"abrechnungsmonat": month})

    print(f"\nDeleted:")
    print(f"  care_events:          {r1.deleted_count}")
    print(f"  care_event_history:   {r2.deleted_count + r3.deleted_count}")
    print(f"  billing_summary:      {r4.deleted_count}")
    print("\nDone. You can now re-import from scratch.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/delete_month.py <MMYYYY> [--dry-run]")
        sys.exit(1)

    month_arg = sys.argv[1]
    is_dry_run = "--dry-run" in sys.argv

    delete_month(month_arg, dry_run=is_dry_run)
