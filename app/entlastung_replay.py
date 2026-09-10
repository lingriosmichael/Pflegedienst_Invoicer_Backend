"""Entlastungsleistung opening-snapshot, dry-run replay, and reviewed-checkpoint support.

Applying a checkpoint is deliberately scoped narrower than the original plan's
fully automatic annual-balance inference (see the RZH reconciliation
implementation plan §14-16 vs. its own §41.2 appendix): a matched RZH
maximum-limit item never corrects the ledger by itself. Staff must first
confirm the private amount (`app.rzh_reconciliation.confirm_private_amount`,
Phase 2) and then separately review and approve the resulting ledger
checkpoint here (Phase 3). Applying one corrects only that patient's
aggregate `entlastung_year_balance` row so future billing consumes an
accurate balance -- it never rewrites other, already-billed events or their
invoices; each of those goes through this same reviewed-approval step
independently as its own RZH evidence arrives.
"""

from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import DEFAULT_ORG_ID
from app.db.mongodb_config import get_database
from app.db.transactions import transactional
from app.entlastung_balance import GO_LIVE_MONTH, LEGACY_COVERAGE_CAP, MONTHLY_CREDIT, SEED_THROUGH_MONTH, get_balance
from app.invoice_eligibility import CONFIRMED_ENTLASTUNG_STATUSES
from app.utils.parsing import generate_id
from app.utils.validation import money, parse_service_date, validate_month


OPENING_COLLECTION = "entlastung_opening_snapshots"
OPENING_LOGIC_VERSION = "legacy_seed_2026_snapshot_v1"
CHECKPOINT_LOGIC_VERSION = "entlastung_reviewed_checkpoint_v1"


def _month_range(start: int, end: int, year: int) -> list[str]:
    return [f"{month:02d}{year}" for month in range(start, end + 1)]


def _later_month(a: str, b: str) -> str:
    """Chronological max of two MMYYYY strings.

    Plain string/Python max() compares lexicographically, which is wrong
    across a year boundary (e.g. "012027" < "072026" as strings even though
    January 2027 is later than July 2026) since the month, not the year,
    is the leading component of this format.
    """
    return a if (int(a[2:]), int(a[:2])) >= (int(b[2:]), int(b[:2])) else b


def _event_sort_key(event: dict) -> tuple:
    fallback = f"01.{event['invoicing_month'][:2]}.{event['invoicing_month'][2:]}"
    try:
        start = parse_service_date(event.get("period_start_date") or fallback)
    except ValueError:
        start = parse_service_date(fallback)
    try:
        end = parse_service_date(event.get("period_end_date") or fallback)
    except ValueError:
        end = start
    return start, end, str(event.get("source_invoice_number") or "~"), str(event.get("care_event_id"))


def opening_snapshot(patient_id: str, database=None) -> dict | None:
    database = database if database is not None else get_database()
    return database[OPENING_COLLECTION].find_one({"org_id": DEFAULT_ORG_ID, "patient_id": patient_id,
                                                   "entitlement_year": 2026})


def freeze_opening_snapshots(dry_run: bool = True, database=None) -> dict:
    """Create immutable June-2026 snapshots without modifying annual ledgers."""
    database = database if database is not None else get_database()
    month_values = _month_range(1, 6, 2026)
    created, existing, proposals = 0, 0, []
    patient_ids = database.patient_profiles.distinct("patient_id", {"org_id": DEFAULT_ORG_ID})
    for patient_id in patient_ids:
        if opening_snapshot(patient_id, database):
            existing += 1
            continue
        events = database.care_events.find({"org_id": DEFAULT_ORG_ID, "patient_id": patient_id,
                                             "event_type": "Entleistung", "invoicing_month": {"$in": month_values}})
        opening_used = money(sum(min(money(event.get("sum_total", 0)), LEGACY_COVERAGE_CAP) for event in events))
        opening_accrued = money(6 * MONTHLY_CREDIT)
        opening_remaining = money(max(0, opening_accrued - opening_used))
        historical_excess_usage = money(max(0, opening_used - opening_accrued))
        proposal = {"org_id": DEFAULT_ORG_ID, "patient_id": patient_id, "entitlement_year": 2026,
                    "opening_as_of": "2026-06-30", "opening_locked": True, "opening_logic_version": OPENING_LOGIC_VERSION,
                    "opening_accrued_amount": opening_accrued, "opening_used_amount": opening_used,
                    "opening_remaining_amount": opening_remaining, "historical_excess_usage": historical_excess_usage}
        proposals.append(proposal)
        if not dry_run:
            database[OPENING_COLLECTION].insert_one({**proposal, "created_at": datetime.now(timezone.utc)})
            created += 1
    return {"dry_run": dry_run, "created": created, "existing": existing, "proposals": proposals}


def replay_patient_dry_run(patient_id: str, target_month: str, database=None) -> dict:
    """Replay July-forward allocation from a locked snapshot without writes.

    Confirmed claim-level coverage is represented faithfully. If it conflicts
    with the estimated balance, the report exposes the discrepancy rather than
    inventing a checkpoint adjustment.
    """
    validate_month(target_month)
    if target_month < GO_LIVE_MONTH or target_month[2:] != "2026":
        raise HTTPException(422, "Dry-run replay currently supports July-December 2026 only")
    database = database if database is not None else get_database()
    snapshot = opening_snapshot(patient_id, database)
    if not snapshot:
        raise HTTPException(409, "Create and review the locked June opening snapshot before replay")
    months = _month_range(7, int(target_month[:2]), 2026)
    events = list(database.care_events.find({"org_id": DEFAULT_ORG_ID, "patient_id": patient_id,
                                              "event_type": "Entleistung", "invoicing_month": {"$in": months}}))
    events.sort(key=_event_sort_key)
    bills = {bill["care_event_id"]: bill for bill in database.billing_details.find(
        {"org_id": DEFAULT_ORG_ID, "care_event_id": {"$in": [event["care_event_id"] for event in events]}})}
    balance = snapshot["opening_remaining_amount"]
    accrued = snapshot["opening_accrued_amount"]
    rows, month_cursor = [], 7
    for event in events:
        event_month = int(event["invoicing_month"][:2])
        while month_cursor <= event_month:
            balance = money(balance + MONTHLY_CREDIT)
            accrued = money(accrued + MONTHLY_CREDIT)
            month_cursor += 1
        total = money(event.get("sum_total", 0))
        estimated_covered = money(min(total, max(balance, 0)))
        bill = bills.get(event["care_event_id"])
        confirmed = bill and bill.get("reconciliation_status") in CONFIRMED_ENTLASTUNG_STATUSES
        effective_covered = money(bill["sum_covered"]) if confirmed else estimated_covered
        # effective_covered can exceed the event's live total (e.g. the event's
        # amount was edited down after confirmation), which would make this
        # negative -- money() asserts nonnegative, so round() directly instead.
        effective_owed = round(total - effective_covered, 2)
        before = balance
        balance = money(balance - effective_covered) if balance >= effective_covered else round(balance - effective_covered, 2)
        rows.append({"care_event_id": event["care_event_id"], "invoicing_month": event["invoicing_month"],
                     "sum_total": total, "estimated_sum_covered": estimated_covered,
                     "estimated_amount_owed": money(total - estimated_covered), "effective_sum_covered": effective_covered,
                     "effective_amount_owed": effective_owed, "coverage_source": "rzh_confirmed" if confirmed else "ledger_estimate",
                     "balance_before": before, "balance_after": balance,
                     "checkpoint_review_delta": money(abs(balance)) if balance < 0 else 0.0})
    while month_cursor <= int(target_month[:2]):
        balance = money(balance + MONTHLY_CREDIT)
        accrued = money(accrued + MONTHLY_CREDIT)
        month_cursor += 1
    return {"patient_id": patient_id, "target_month": target_month, "dry_run": True,
            "opening_remaining_amount": snapshot["opening_remaining_amount"], "historical_excess_usage": snapshot["historical_excess_usage"],
            "proposed_accrued_amount": accrued, "proposed_remaining_amount": balance, "events": rows,
            "requires_checkpoint_review": any(row["checkpoint_review_delta"] for row in rows)}


def reference_year_trajectory(patient_id: str, database=None, year: int = 2026) -> dict[str, dict]:
    """Pure statutory Sec.45b reference calculation, January through December.

    Ignores both the legacy per-event-cap approximation billed for Jan-Jun and
    the operational July-forward ledger/checkpoints entirely: starts from a
    zero balance on 1 January, credits MONTHLY_CREDIT for every elapsed
    calendar month, and consumes it chronologically as each Entleistung 4064
    event's own sum_total is reached -- the plain "unused rolls forward"
    monthly-accrual rule, with no cap and no RZH-checkpoint override.

    Read-only and for staff display/audit only. Never written anywhere and
    never used to alter a billing_detail, invoice, or the operational
    entlastung_year_balance ledger -- see the 2026-09-09 decision to never
    correct January-June 2026 history through this feature.
    """
    database = database if database is not None else get_database()
    months = _month_range(1, 12, year)
    # care_account is filtered in Python, not as a Mongo-level exact match --
    # matching the defensive str() cast used everywhere else this field is
    # read (e.g. app/rzh_reconciliation.py::match_item) since it is not
    # guaranteed to already be stored as the literal string "4064".
    events = [event for event in database.care_events.find({"org_id": DEFAULT_ORG_ID, "patient_id": patient_id,
        "event_type": "Entleistung", "invoicing_month": {"$in": months}})
        if str(event.get("care_account", "")).strip() == "4064"]
    events.sort(key=_event_sort_key)
    balance, month_cursor, trajectory = 0.0, 1, {}
    for event in events:
        event_month = int(event["invoicing_month"][:2])
        while month_cursor <= event_month:
            balance = money(balance + MONTHLY_CREDIT)
            month_cursor += 1
        total = money(event.get("sum_total", 0))
        available_before = balance
        covered = money(min(total, max(balance, 0)))
        balance = money(balance - covered)
        trajectory[event["care_event_id"]] = {
            "invoicing_month": event["invoicing_month"], "available_before": available_before,
            "sum_total": total, "reference_covered": covered, "reference_owed": money(total - covered),
            "balance_after": balance,
        }
    return trajectory


def _corrected_forward_replay(patient_id: str, target_month: str, database, force_applied_item_id: str | None = None) -> dict:
    """Recompute the July-forward ledger, applying the plan's checkpoint
    correction for every event whose confirming reconciliation item's ledger
    checkpoint is already applied (or, while staff are approving one right
    now, `force_applied_item_id`): top the balance up to the RZH-confirmed
    covered amount, then consume it -- instead of the plain dry-run above,
    which deliberately lets the balance run negative to surface the
    discrepancy rather than correct it. This function is the only place that
    actually corrects the balance, and only ever for events with an approved
    checkpoint.
    """
    snapshot = opening_snapshot(patient_id, database)
    if not snapshot:
        raise HTTPException(409, "Create and review the locked June opening snapshot before replay")
    months = _month_range(7, int(target_month[:2]), 2026)
    events = list(database.care_events.find({"org_id": DEFAULT_ORG_ID, "patient_id": patient_id,
                                              "event_type": "Entleistung", "invoicing_month": {"$in": months}}))
    events.sort(key=_event_sort_key)
    bills = {bill["care_event_id"]: bill for bill in database.billing_details.find(
        {"org_id": DEFAULT_ORG_ID, "care_event_id": {"$in": [event["care_event_id"] for event in events]}})}
    balance = snapshot["opening_remaining_amount"]
    accrued = snapshot["opening_accrued_amount"]
    used = snapshot["opening_used_amount"]
    rows, month_cursor = [], 7
    for event in events:
        event_month = int(event["invoicing_month"][:2])
        while month_cursor <= event_month:
            balance = money(balance + MONTHLY_CREDIT)
            accrued = money(accrued + MONTHLY_CREDIT)
            month_cursor += 1
        total = money(event.get("sum_total", 0))
        bill = bills.get(event["care_event_id"])
        item = None
        if bill and bill.get("reconciliation_item_id"):
            item = database.rzh_reconciliation_items.find_one({"org_id": DEFAULT_ORG_ID,
                "reconciliation_item_id": bill["reconciliation_item_id"]})
        # "reviewed" (private amount confirmed, Phase 2) is checkpoint-eligible
        # here too, not just "applied" -- the confirmed amount is already a
        # staff-reviewed fact regardless of whether ITS OWN ledger checkpoint
        # has been separately applied yet. Restricting this to "applied" only
        # would silently fall back to the stale ledger estimate for any earlier
        # event whose checkpoint staff hasn't gotten to yet, producing a wrong
        # available_before for whichever event IS being applied right now.
        is_applied_checkpoint = bool(item and (item.get("apply_status") in {"reviewed", "applied"}
                                                or item.get("reconciliation_item_id") == force_applied_item_id))
        available_before = balance
        if is_applied_checkpoint:
            confirmed_covered = money(bill["sum_covered"])
            # This delta is expected to go negative -- that's the whole point
            # of a checkpoint (RZH confirming a LOWER available amount than the
            # ledger estimated). money() asserts nonnegative, so use round().
            checkpoint_delta = round(confirmed_covered - balance, 2)
            balance = money(balance + checkpoint_delta)
            covered = confirmed_covered
        else:
            checkpoint_delta = 0.0
            covered = money(min(total, max(balance, 0)))
        owed = money(total - covered)
        balance = money(balance - covered)
        used = money(used + covered)
        rows.append({"care_event_id": event["care_event_id"], "billing_detail_id": bill["billing_detail_id"] if bill else None,
                     "reconciliation_item_id": item.get("reconciliation_item_id") if item else None,
                     "invoicing_month": event["invoicing_month"], "available_before": available_before,
                     "checkpoint_delta": checkpoint_delta, "sum_covered": covered, "amount_owed": owed,
                     "balance_after": balance})
    while month_cursor <= int(target_month[:2]):
        balance = money(balance + MONTHLY_CREDIT)
        accrued = money(accrued + MONTHLY_CREDIT)
        month_cursor += 1
    return {"accrued_amount": accrued, "used_amount": used, "remaining_amount": balance,
            "credited_through_month": target_month, "rows": rows}


def _checkpoint_target(reconciliation_item_id: str, database) -> tuple[dict, dict, dict]:
    """Load and validate the item/event/billing-detail triple a checkpoint acts on."""
    item = database.rzh_reconciliation_items.find_one({"org_id": DEFAULT_ORG_ID,
                                                         "reconciliation_item_id": reconciliation_item_id})
    if not item:
        raise HTTPException(404, "Reconciliation item not found")
    if item.get("apply_status") not in {"reviewed", "applied"}:
        raise HTTPException(409, "Confirm the private amount before reviewing a ledger checkpoint")
    event = database.care_events.find_one({"org_id": DEFAULT_ORG_ID, "care_event_id": item.get("matched_care_event_id")})
    if not event:
        raise HTTPException(404, "Matched care event not found")
    if event.get("invoicing_month", "")[2:] != "2026":
        raise HTTPException(422, "Ledger checkpoints currently support 2026 events only")
    bill = database.billing_details.find_one({"org_id": DEFAULT_ORG_ID, "care_event_id": event["care_event_id"]})
    if not bill or bill.get("reconciliation_item_id") != reconciliation_item_id:
        raise HTTPException(409, "Billing detail is not confirmed by this reconciliation item")
    return item, event, bill


def preview_checkpoint(reconciliation_item_id: str, database=None) -> dict:
    """Read-only preview of what applying this reviewed item's checkpoint would do."""
    database = database if database is not None else get_database()
    item, event, _ = _checkpoint_target(reconciliation_item_id, database)
    patient_id, invoicing_month = event["patient_id"], event["invoicing_month"]
    live_row = get_balance(patient_id, 2026) or {}
    target_month = _later_month(invoicing_month, live_row.get("credited_through_month") or invoicing_month)
    corrected = _corrected_forward_replay(patient_id, target_month, database, force_applied_item_id=reconciliation_item_id)
    row = next((r for r in corrected["rows"] if r["care_event_id"] == event["care_event_id"]), None)
    return {
        "reconciliation_item_id": reconciliation_item_id, "patient_id": patient_id, "care_event_id": event["care_event_id"],
        "already_applied": item.get("apply_status") == "applied",
        "checkpoint_delta": row["checkpoint_delta"] if row else 0.0,
        "current_remaining_amount": live_row.get("remaining_amount"), "current_used_amount": live_row.get("used_amount"),
        "current_credited_through_month": live_row.get("credited_through_month"),
        "proposed_remaining_amount": corrected["remaining_amount"], "proposed_used_amount": corrected["used_amount"],
        "proposed_credited_through_month": corrected["credited_through_month"],
    }


@transactional
def apply_checkpoint(reconciliation_item_id: str, actor: str) -> dict:
    """Apply one staff-reviewed RZH checkpoint to the patient's live ledger.

    Requires the private amount to already be confirmed (apply_status ==
    "reviewed"; see app.rzh_reconciliation.confirm_private_amount). Recomputes
    the patient's July-forward balance deterministically from the locked
    opening snapshot, applying this checkpoint (and any previously applied
    ones) via `_corrected_forward_replay`, then overwrites the ledger's
    aggregate accrued/used/remaining/credited_through_month with that result
    and records an append-only adjustment plus a replay-run audit record.
    """
    database = get_database()
    item, event, bill = _checkpoint_target(reconciliation_item_id, database)
    if item.get("apply_status") == "applied":
        return {"status": "already_applied", "reconciliation_item_id": reconciliation_item_id}

    patient_id, invoicing_month = event["patient_id"], event["invoicing_month"]
    live_row = get_balance(patient_id, 2026)
    if not live_row:
        raise HTTPException(409, "No entlastung_year_balance row exists for this patient/year yet")
    target_month = _later_month(invoicing_month, live_row.get("credited_through_month") or invoicing_month)

    corrected = _corrected_forward_replay(patient_id, target_month, database, force_applied_item_id=reconciliation_item_id)
    row = next((r for r in corrected["rows"] if r["care_event_id"] == event["care_event_id"]), None)
    if row is None:
        raise HTTPException(409, "Replay did not include this event")
    if not row["checkpoint_delta"]:
        raise HTTPException(409, "Replay shows no ledger discrepancy for this event; nothing to apply")

    now = datetime.now(timezone.utc)
    before_remaining = live_row.get("remaining_amount", 0.0)
    before_used = live_row.get("used_amount", 0.0)

    database.entlastung_year_balance.update_one(
        {"org_id": DEFAULT_ORG_ID, "patient_id": patient_id, "entitlement_year": 2026},
        {"$set": {"accrued_amount": corrected["accrued_amount"], "used_amount": corrected["used_amount"],
                  "remaining_amount": corrected["remaining_amount"], "credited_through_month": corrected["credited_through_month"],
                  "logic_version": CHECKPOINT_LOGIC_VERSION, "updated_at": now}})

    replay_run_id = generate_id("err")
    database.entlastung_replay_runs.insert_one({
        "replay_run_id": replay_run_id, "org_id": DEFAULT_ORG_ID, "patient_id": patient_id, "entitlement_year": 2026,
        "target_month": target_month, "trigger": "rzh_reconciliation_checkpoint", "trigger_item_id": reconciliation_item_id,
        "care_event_id": event["care_event_id"], "before_remaining_amount": before_remaining,
        "after_remaining_amount": corrected["remaining_amount"], "before_used_amount": before_used,
        "after_used_amount": corrected["used_amount"], "checkpoint_delta": row["checkpoint_delta"],
        "actor": actor, "created_at": now, "logic_version": CHECKPOINT_LOGIC_VERSION})
    database.entlastung_balance_adjustments.insert_one({
        "adjustment_id": generate_id("eba"), "org_id": DEFAULT_ORG_ID, "patient_id": patient_id, "entitlement_year": 2026,
        "amount_cents": round(row["checkpoint_delta"] * 100), "adjustment_type": "rzh_checkpoint",
        "reason_code": item.get("reason_code"), "care_event_id": event["care_event_id"],
        "reconciliation_item_id": reconciliation_item_id, "replay_run_id": replay_run_id, "active": True,
        "created_at": now, "created_by": actor})
    database.rzh_reconciliation_items.update_one({"_id": item["_id"]},
        {"$set": {"apply_status": "applied", "ledger_checkpoint_applied_at": now,
                  "ledger_checkpoint_applied_by": actor, "replay_run_id": replay_run_id, "updated_at": now}})
    database.care_event_history.insert_one({"org_id": DEFAULT_ORG_ID, "care_event_id": event["care_event_id"],
        "action": "entlastung_ledger_checkpoint_applied", "created_at": now, "actor": actor,
        "before": {"remaining_amount": before_remaining, "used_amount": before_used},
        "after": {"remaining_amount": corrected["remaining_amount"], "used_amount": corrected["used_amount"]},
        "reconciliation_item_id": reconciliation_item_id, "replay_run_id": replay_run_id})

    return {"status": "applied", "reconciliation_item_id": reconciliation_item_id, "replay_run_id": replay_run_id,
            "checkpoint_delta": row["checkpoint_delta"], "before_remaining_amount": before_remaining,
            "after_remaining_amount": corrected["remaining_amount"], "before_used_amount": before_used,
            "after_used_amount": corrected["used_amount"], "credited_through_month": corrected["credited_through_month"]}
