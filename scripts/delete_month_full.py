import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.deletion import delete_month_records
from app.db import DEFAULT_ORG_ID


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("month")
    parser.add_argument("--org-id", default=DEFAULT_ORG_ID)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true")
    options = parser.parse_args()
    report = delete_month_records(options.month, dry_run=True, org_id=options.org_id)
    print(json.dumps(report, indent=2))
    if options.dry_run:
        return
    if not options.yes and input("Delete these unissued records? Type yes: ").strip() != "yes":
        return
    print(json.dumps(delete_month_records(options.month, dry_run=False, org_id=options.org_id), indent=2))


if __name__ == "__main__":
    main()
