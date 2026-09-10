"""Normalize canonical patient-profile names while preserving source evidence.

Run without --apply to report the number of affected profiles. This script
changes `patient_profiles.patient_name` only; it never changes RZH raw PDF
evidence, care events, invoices, or insurance identifiers.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import DEFAULT_ORG_ID
from app.db.mongodb_config import get_database
from app.utils.data_validation import DataNormalizer


def run(*, apply: bool) -> dict:
    database = get_database()
    candidates = []
    for profile in database.patient_profiles.find(
        {"org_id": DEFAULT_ORG_ID}, {"patient_id": 1, "patient_name": 1}
    ):
        original = profile.get("patient_name")
        normalized = DataNormalizer.normalize_patient_name(original)
        if normalized != original:
            candidates.append((profile["_id"], normalized))

    result = {"dry_run": not apply, "profiles_requiring_normalization": len(candidates), "updated": 0}
    if not apply:
        return result

    now = datetime.now(timezone.utc)
    for profile_id, normalized in candidates:
        database.patient_profiles.update_one({"_id": profile_id}, {"$set": {
            "patient_name": normalized,
            "name_normalized_at": now,
            "updated_at": now,
        }})
        result["updated"] += 1
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="persist normalized profile names")
    options = parser.parse_args()
    print(json.dumps(run(apply=options.apply), indent=2, default=str))
