"""
Entlastungsleistung (§45b SGB XI) year-bucket balance tracking.

Replaces the old monthly (>127.35 EUR) / yearly-cumulative (>1500 EUR) rules
with a real balance: each calendar year gets its own bucket, credited 131 EUR
per month, consumed as Entlastungsleistung services are billed. Months before
GO_LIVE_MONTH stay on the old rules untouched (see mark_month_ready_for_generation).

See pflegedienst_gui/ENTLASTUNGSLEISTUNG_ROLLOUT.md for the full spec.
"""

import logging
from datetime import datetime
from typing import Optional, Dict, Any

from app.db.mongodb_config import get_database
from app.db import DEFAULT_ORG_ID
from app.db.transactions import transactional
from app.utils.validation import validate_month, money

logger = logging.getLogger(__name__)

COLLECTION = "entlastung_year_balance"
LOGIC_VERSION = "entlastung_bucket_v1"

MONTHLY_CREDIT = 131.00

# First month billed under the new balance-based logic. Everything before this
# stays on the old per-event cap / yearly-cumulative rules, untouched.
GO_LIVE_MONTH = "072026"

# Last month under the old rules; the 2026 bucket is seeded with this many
# months of accrual (Jan-Jun) and this much historical usage.
SEED_THROUGH_MONTH = "062026"

# Cap used by the old per-event rule, needed to reconstruct historical usage
# for months before GO_LIVE_MONTH when seeding a bucket's used_amount.
LEGACY_COVERAGE_CAP = 127.35


def _month_num(mmYYYY: str) -> int:
    return int(validate_month(mmYYYY)[:2])


def _entitlement_year(mmYYYY: str) -> int:
    return int(mmYYYY[2:])


def _expires_on_for_year(year: int) -> datetime:
    return datetime(year + 1, 6, 30)


def uses_new_entlastung_logic(invoicing_month: str) -> bool:
    """True if this month should use balance-based coverage instead of the old rules."""
    return (_entitlement_year(invoicing_month), _month_num(invoicing_month)) >= (
        _entitlement_year(GO_LIVE_MONTH),
        _month_num(GO_LIVE_MONTH),
    )


def get_balance(patient_id: str, entitlement_year: int) -> Optional[Dict[str, Any]]:
    db = get_database()
    return db[COLLECTION].find_one({
        "org_id": DEFAULT_ORG_ID,
        "patient_id": patient_id,
        "entitlement_year": entitlement_year,
    })


def _create_balance_row(
    patient_id: str,
    entitlement_year: int,
    credited_through_month: Optional[str],
    accrued_amount: float,
    used_amount: float,
) -> Dict[str, Any]:
    db = get_database()
    now = datetime.utcnow()
    doc = {
        "org_id": DEFAULT_ORG_ID,
        "patient_id": patient_id,
        "entitlement_year": entitlement_year,
        "credited_through_month": credited_through_month,
        "accrued_amount": round(accrued_amount, 2),
        "used_amount": round(used_amount, 2),
        "remaining_amount": round(max(0.0, accrued_amount - used_amount), 2),
        "manual_adjustment": 0.0,
        "manual_adjustment_note": None,
        "expires_on": _expires_on_for_year(entitlement_year),
        "logic_version": LOGIC_VERSION,
        "created_at": now,
        "updated_at": now,
    }
    db[COLLECTION].insert_one(doc)
    return doc


@transactional
def ensure_entlastung_credit_through(patient_id: str, target_month: str) -> Dict[str, Any]:
    """
    Make sure the patient's bucket for target_month's entitlement year has been
    credited with 131 EUR for every month up to and including target_month.

    Idempotent: safe to call on every backend startup and before every
    Entlastungsleistung processing/generation pass, any number of times.
    """
    db = get_database()
    year = _entitlement_year(target_month)
    row = get_balance(patient_id, year)

    if row is None:
        if year == 2026 and _month_num(target_month) >= 7:
            months = [f"{month:02d}2026" for month in range(1, 7)]
            events = db.care_events.find({"org_id": DEFAULT_ORG_ID, "patient_id": patient_id,
                                         "event_type": "Entleistung", "invoicing_month": {"$in": months}})
            used = sum(min(money(event.get("sum_total", 0)), LEGACY_COVERAGE_CAP) for event in events)
            row = _create_balance_row(patient_id, year, "062026", 786.0, used)
        else:
            row = _create_balance_row(patient_id, year, None, 0.0, 0.0)

    credited_month_num = _month_num(row["credited_through_month"]) if row.get("credited_through_month") else 0
    target_month_num = _month_num(target_month)
    missing_months = target_month_num - credited_month_num

    if missing_months <= 0:
        return row

    credit_to_add = round(missing_months * MONTHLY_CREDIT, 2)
    updated = db[COLLECTION].find_one_and_update(
        {"org_id": DEFAULT_ORG_ID, "patient_id": patient_id, "entitlement_year": year},
        {
            "$inc": {
                "accrued_amount": credit_to_add,
                "remaining_amount": credit_to_add,
            },
            "$set": {
                "credited_through_month": target_month,
                "updated_at": datetime.utcnow(),
            },
        },
        return_document=True,
    )
    logger.info(
        "Credited %.2f EUR (%d month(s)) to patient %s entitlement_year %d, now credited through %s",
        credit_to_add, missing_months, patient_id, year, target_month,
    )
    return updated


@transactional
def apply_entlastung_usage(patient_id: str, invoicing_month: str, sum_total: float) -> Dict[str, float]:
    """
    Consume balance for one Entlastungsleistung event. Always mutates the
    balance (used/remaining), regardless of whether the resulting owed amount
    ends up being invoiced.

    Returns {"covered": ..., "owed": ...}.
    """
    db = get_database()
    ensure_entlastung_credit_through(patient_id, invoicing_month)
    year = _entitlement_year(invoicing_month)
    row = get_balance(patient_id, year)

    sum_total = money(sum_total)
    remaining = max(0.0, row["remaining_amount"])
    covered = round(min(sum_total, remaining), 2)
    owed = round(sum_total - covered, 2)

    db[COLLECTION].update_one(
        {"org_id": DEFAULT_ORG_ID, "patient_id": patient_id, "entitlement_year": year},
        {
            "$inc": {"used_amount": covered, "remaining_amount": -covered},
            "$set": {"updated_at": datetime.utcnow()},
        },
    )
    return {"covered": covered, "owed": owed}


@transactional
def reverse_entlastung_usage(patient_id: str, invoicing_month: str, previously_covered: float) -> None:
    """Undo a previous apply_entlastung_usage() call, before recomputing an edited event."""
    previously_covered = money(previously_covered)
    if not previously_covered:
        return
    db = get_database()
    year = _entitlement_year(invoicing_month)
    db[COLLECTION].update_one(
        {"org_id": DEFAULT_ORG_ID, "patient_id": patient_id, "entitlement_year": year},
        {
            "$inc": {"used_amount": -previously_covered, "remaining_amount": previously_covered},
            "$set": {"updated_at": datetime.utcnow()},
        },
    )


@transactional
def recompute_entlastung_for_care_event(
    patient_id: str, invoicing_month: str, new_sum_total: float, previously_covered: float
) -> Dict[str, float]:
    """Reverse a previous usage application and reapply with an edited sum_total."""
    reverse_entlastung_usage(patient_id, invoicing_month, previously_covered)
    return apply_entlastung_usage(patient_id, invoicing_month, new_sum_total)


@transactional
def seed_entlastung_2026_balances(through_month: str = SEED_THROUGH_MONTH) -> Dict[str, int]:
    """
    Create the 2026 bucket for every patient that doesn't already have one,
    reconstructing used_amount from Jan-through_month historical Entleistung
    care_events (each capped at LEGACY_COVERAGE_CAP, matching the old rule).

    Idempotent: never overwrites an existing row, safe to call on every startup.
    """
    db = get_database()
    year = _entitlement_year(through_month)
    accrued = round(_month_num(through_month) * MONTHLY_CREDIT, 2)

    months_in_range = [f"{m:02d}{year}" for m in range(1, _month_num(through_month) + 1)]

    seeded = 0
    skipped = 0
    patient_ids = db.patient_profiles.distinct("patient_id", {"org_id": DEFAULT_ORG_ID})

    for patient_id in patient_ids:
        if get_balance(patient_id, year) is not None:
            skipped += 1
            continue

        historical_events = db.care_events.find({
            "org_id": DEFAULT_ORG_ID,
            "patient_id": patient_id,
            "event_type": "Entleistung",
            "invoicing_month": {"$in": months_in_range},
        })
        used_amount = sum(
            min(float(ce.get("sum_total", 0) or 0), LEGACY_COVERAGE_CAP)
            for ce in historical_events
        )

        _create_balance_row(
            patient_id, year,
            credited_through_month=through_month,
            accrued_amount=accrued,
            used_amount=used_amount,
        )
        seeded += 1

    logger.info(
        "Entlastungsleistung %d bucket seed: %d created, %d already existed (of %d patients)",
        year, seeded, skipped, len(patient_ids),
    )
    return {"seeded": seeded, "skipped_existing": skipped, "total_patients": len(patient_ids)}
