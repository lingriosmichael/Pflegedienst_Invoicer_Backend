#!/usr/bin/env python3
"""Rebuild the independent PDF audit table (pdf_audit_documents/pdf_audit_lines)
for one calendar year from data/abrechnung/<year>/*.pdf.

Usage: python scripts/build_pdf_audit.py --year 2025
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pdf_audit import run_audit_for_year


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument(
        "--dir", type=Path, default=None,
        help="Defaults to data/abrechnung/<year> relative to the repo root.",
    )
    args = parser.parse_args()

    directory = args.dir or Path(__file__).resolve().parent.parent / "data" / "abrechnung" / str(args.year)
    if not directory.is_dir():
        print(f"No such directory: {directory}", file=sys.stderr)
        sys.exit(1)

    summary = run_audit_for_year(args.year, directory)
    print(f"Year {summary['year']}: {summary['files_processed']} PDFs, "
          f"{summary['total_lines']} lines extracted, "
          f"{summary['files_needing_review']} file(s) flagged for review.")
    for doc in summary["documents"]:
        flag = "REVIEW" if doc["review_flag"] else "ok"
        print(f"  [{flag}] {doc['source_file']} -> billing_month={doc['detected_billing_month']} "
              f"lines={doc['extracted_line_count']} "
              f"extracted_total={doc['extracted_total_amount']} "
              f"source_total={doc['source_total_amount']}")


if __name__ == "__main__":
    main()
