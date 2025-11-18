#!/usr/bin/env python3
"""Backfill include_service_packet for patients matching known insurance numbers.

Run without --apply to preview changes. Use --apply to perform the DB updates.
"""
import argparse
import logging
from app.db.connection import get_db

logger = logging.getLogger(__name__)

HARDCODED_INSURANCES = [
    "G052782637",
    "P815949722",
    "I206016241",
    "E813094835",
]


def preview_changes():
    with get_db() as conn:
        c = conn.cursor()
        # Ensure column exists (older DBs may not have it yet)
        c.execute("PRAGMA table_info(patients)")
        cols = [r[1] for r in c.fetchall()]
        if "include_service_packet" not in cols:
            # Add the column so we can backfill
            c.execute("ALTER TABLE patients ADD COLUMN include_service_packet INTEGER DEFAULT 0")
            conn.commit()
            logger.info("Added include_service_packet column to patients table")
        q = "SELECT id, name, insurance_number, include_service_packet FROM patients WHERE insurance_number IN ({})".format(
            ",".join(["?" for _ in HARDCODED_INSURANCES])
        )
        c.execute(q, HARDCODED_INSURANCES)
        rows = c.fetchall()
        return rows


def apply_updates():
    with get_db() as conn:
        c = conn.cursor()
        q = "UPDATE patients SET include_service_packet = 1 WHERE insurance_number IN ({})".format(
            ",".join(["?" for _ in HARDCODED_INSURANCES])
        )
        c.execute(q, HARDCODED_INSURANCES)
        conn.commit()
        return c.rowcount


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply updates to the database")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    rows = preview_changes()
    if not rows:
        logger.info("No patients found matching the hard-coded insurance numbers.")
    else:
        logger.info("Found the following patients matching the hard-coded list:")
        for r in rows:
            pid, name, ins, flag = r
            logger.info(f" - id={pid} name={name!r} insurance={ins!r} include_service_packet={flag}")

    if args.apply:
        count = apply_updates()
        logger.info(f"Applied updates: {count} rows updated.")
    else:
        logger.info("Dry-run mode: no changes applied. Re-run with --apply to perform updates.")


if __name__ == '__main__':
    main()
