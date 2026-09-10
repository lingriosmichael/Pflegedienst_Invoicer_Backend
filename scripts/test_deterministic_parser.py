#!/usr/bin/env python3
"""
Standalone test harness for app/deterministic_parser.py.

Runs the deterministic (non-LLM) parser against real RZH PDFs and reports:
  - per-PDF success/failure counts
  - the exact reason for every failed chunk (so gaps in the grammar are
    obvious and fixable, rather than silently swallowed)
  - a self-consistency check on every successfully parsed record, using the
    SAME invariants app.import_records.persist_record enforces at import
    time (services reconcile to the invoice total, coverage <= total, dates
    ordered, birthdate normalizable) -- without writing anything to the
    database and without calling OpenAI.

This script does not touch the live import pipeline, the database, or the
OpenAI client. It exists purely so the deterministic parser's output can be
inspected and trusted before anyone wires it into app/pdf_parser.py.

Usage:
    python scripts/test_deterministic_parser.py                 # all PDFs in data/abrechnung
    python scripts/test_deterministic_parser.py path/to/one.pdf  # a single PDF
    python scripts/test_deterministic_parser.py --json out.json  # dump full results as JSON
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.deterministic_parser import parse_pdf
from app.utils.parsing import GermanDecimalParser
from app.utils.data_validation import DataNormalizer, DataValidationError
from app.utils.validation import parse_service_date


def self_validate(record):
    """Re-check a successfully parsed record against the same invariants
    app.import_records.persist_record enforces at import time. Returns a
    reason string if invalid, else None."""
    patient, invoice, services = record["patient"], record["invoice"], record["services"]

    if not patient.get("name", "").strip() or not patient.get("insurance_number", "").strip():
        return "patient identity incomplete"

    try:
        birthdate = DataNormalizer.normalize_birthdate(patient.get("birthdate"))
        parse_service_date(birthdate)
    except (DataValidationError, ValueError) as error:
        return f"birthdate invalid: {error}"

    try:
        total = GermanDecimalParser.parse(invoice["summe_total"])
        covered = GermanDecimalParser.parse(invoice["summe_covered"])
    except ValueError as error:
        return f"invoice amount invalid: {error}"

    if covered > total + 0.01:
        return f"coverage ({covered}) exceeds total ({total})"

    try:
        start = parse_service_date(invoice["pflegezeitraum_beginn"])
        end = parse_service_date(invoice["pflegezeitraum_ende"])
    except ValueError as error:
        return f"service period invalid: {error}"
    if start > end:
        return "service period reversed"

    if not services:
        return "no services"
    try:
        line_sum = sum(GermanDecimalParser.parse(service["total_price"]) for service in services)
    except ValueError as error:
        return f"service amount invalid: {error}"
    if abs(line_sum - total) > 0.01:
        return f"service lines ({line_sum:.2f}) do not reconcile to invoice total ({total:.2f})"

    return None


def run(pdf_path):
    results = parse_pdf(str(pdf_path))
    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] == "failed"]

    invalid = []
    for r in ok:
        reason = self_validate(r["record"])
        if reason:
            invalid.append({**r, "invalid_reason": reason})

    clean = len(ok) - len(invalid)
    print(f"\n{pdf_path.name}")
    print(f"  chunks: {len(results)}  parsed: {len(ok)}  parse_failed: {len(failed)}  "
          f"self_validation_failed: {len(invalid)}  clean: {clean}")

    for r in failed:
        print(f"  [PARSE FAIL]  chunk {r['index']}: {r['error']}")
    for r in invalid:
        name = r["record"]["patient"].get("name", "?")
        print(f"  [INVALID]     chunk {r['index']} ({name}): {r['invalid_reason']}")

    return {"file": pdf_path.name, "results": results, "invalid": invalid,
            "counts": {"chunks": len(results), "parsed": len(ok), "parse_failed": len(failed),
                       "self_validation_failed": len(invalid), "clean": clean}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", nargs="?", help="single PDF to test; defaults to all of data/abrechnung/*.pdf")
    parser.add_argument("--json", help="write full results (including parsed records) to this file")
    args = parser.parse_args()

    if args.pdf:
        pdfs = [Path(args.pdf)]
    else:
        pdfs = sorted(Path("data/abrechnung").glob("*.pdf"))

    if not pdfs:
        print("No PDFs found.")
        return

    all_reports = [run(pdf) for pdf in pdfs]

    total_chunks = sum(r["counts"]["chunks"] for r in all_reports)
    total_clean = sum(r["counts"]["clean"] for r in all_reports)
    print(f"\n=== TOTAL: {total_clean}/{total_chunks} chunks parsed and self-validated cleanly "
          f"({total_clean / total_chunks:.1%}) ===" if total_chunks else "No chunks found.")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(all_reports, fh, ensure_ascii=False, indent=2)
        print(f"Full results written to {args.json}")


if __name__ == "__main__":
    main()
