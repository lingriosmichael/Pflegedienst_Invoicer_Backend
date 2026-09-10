import mongomock
import pytest

from fastapi import HTTPException

from app.db import mongodb_config
from app.entlastung_balance import MONTHLY_CREDIT
from app.entlastung_replay import (_later_month, apply_checkpoint, freeze_opening_snapshots, preview_checkpoint,
                                    reference_year_trajectory, replay_patient_dry_run)


def test_later_month_compares_chronologically_across_a_year_boundary():
    """Regression test: plain string max() on 'MMYYYY' is wrong across a year
    boundary since month, not year, leads the string (e.g. '012027' < '072026'
    lexicographically despite January 2027 being later than July 2026).
    """
    assert _later_month("072026", "092026") == "092026"
    assert _later_month("092026", "072026") == "092026"
    assert _later_month("072026", "012027") == "012027"
    assert _later_month("122026", "012027") == "012027"
    assert _later_month("072026", "072026") == "072026"


def test_opening_snapshot_preserves_historical_excess_without_touching_ledger(monkeypatch):
    database = mongomock.MongoClient().test
    monkeypatch.setattr(mongodb_config, "_database", database)
    database.patient_profiles.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "patient_name": "Synthetic", "insurance_number": "SYNTHETIC"})
    for month in range(1, 7):
        for sequence in range(2):
            database.care_events.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "care_event_id": f"evt_{month}_{sequence}",
                "event_type": "Entleistung", "invoicing_month": f"{month:02d}2026", "sum_total": 200.0})
    database.entlastung_year_balance.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "entitlement_year": 2026,
        "remaining_amount": 0.0})

    preview = freeze_opening_snapshots(dry_run=True)
    assert preview["created"] == 0
    assert preview["proposals"][0]["opening_remaining_amount"] == 0.0
    assert preview["proposals"][0]["historical_excess_usage"] == 742.2
    assert database.entlastung_opening_snapshots.count_documents({}) == 0
    assert database.entlastung_year_balance.find_one()["remaining_amount"] == 0.0

    applied = freeze_opening_snapshots(dry_run=False)
    assert applied["created"] == 1
    assert database.entlastung_year_balance.find_one()["remaining_amount"] == 0.0


def test_dry_run_reports_confirmed_coverage_conflict_without_checkpoint_write(monkeypatch):
    database = mongomock.MongoClient().test
    monkeypatch.setattr(mongodb_config, "_database", database)
    database.patient_profiles.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "patient_name": "Synthetic", "insurance_number": "SYNTHETIC"})
    database.entlastung_opening_snapshots.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "entitlement_year": 2026,
        "opening_as_of": "2026-06-30", "opening_locked": True, "opening_logic_version": "test", "opening_accrued_amount": 786.0,
        "opening_used_amount": 786.0, "opening_remaining_amount": 0.0, "historical_excess_usage": 0.0})
    database.care_events.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "care_event_id": "evt_july",
        "event_type": "Entleistung", "invoicing_month": "072026", "sum_total": 212.25,
        "period_start_date": "01.07.2026", "period_end_date": "31.07.2026"})
    database.billing_details.insert_one({"org_id": "org_default", "billing_detail_id": "bill_july", "care_event_id": "evt_july",
        "sum_covered": 200.0, "amount_owed": 12.25, "reconciliation_status": "confirmed_limit_partial"})

    report = replay_patient_dry_run("pat_synthetic", "072026")

    assert report["dry_run"] is True
    assert report["requires_checkpoint_review"] is True
    assert report["events"][0]["balance_after"] == -69.0
    assert database.entlastung_year_balance.count_documents({}) == 0


def _reviewed_checkpoint_fixture(database):
    """A staff-confirmed (Phase 2) but not-yet-ledger-applied Entlastung shortfall."""
    database.patient_profiles.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic",
        "patient_name": "Synthetic", "insurance_number": "SYNTHETIC"})
    database.entlastung_opening_snapshots.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic",
        "entitlement_year": 2026, "opening_as_of": "2026-06-30", "opening_locked": True,
        "opening_logic_version": "test", "opening_accrued_amount": 786.0, "opening_used_amount": 786.0,
        "opening_remaining_amount": 0.0, "historical_excess_usage": 0.0})
    database.care_events.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "care_event_id": "evt_july",
        "event_type": "Entleistung", "care_account": "4064", "invoicing_month": "072026", "sum_total": 212.25,
        "period_start_date": "01.07.2026", "period_end_date": "31.07.2026"})
    database.billing_details.insert_one({"org_id": "org_default", "billing_detail_id": "bill_july",
        "care_event_id": "evt_july", "invoicing_month": "072026", "sum_total": 212.25, "sum_covered": 200.0,
        "amount_owed": 12.25, "billing_status": "invoice_needed", "reconciliation_status": "confirmed_limit_partial",
        "confirmed_sum_covered": 200.0, "confirmed_amount_owed": 12.25, "reconciliation_item_id": "rri_july"})
    database.entlastung_year_balance.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic",
        "entitlement_year": 2026, "accrued_amount": 917.0, "used_amount": 786.0, "remaining_amount": 131.0})
    database.rzh_reconciliation_items.insert_one({"org_id": "org_default", "reconciliation_item_id": "rri_july",
        "item_fingerprint": "synthetic-july", "reason_code": "ENTITLEMENT_EXHAUSTED", "amount_cents": -1225,
        "match_status": "matched", "matched_care_event_id": "evt_july", "apply_status": "reviewed"})


def test_preview_checkpoint_shows_the_correction_without_writing_it(monkeypatch):
    database = mongomock.MongoClient().test
    monkeypatch.setattr(mongodb_config, "_database", database)
    _reviewed_checkpoint_fixture(database)

    preview = preview_checkpoint("rri_july")

    assert preview["already_applied"] is False
    assert preview["checkpoint_delta"] == 69.0
    assert preview["current_remaining_amount"] == 131.0
    assert preview["proposed_remaining_amount"] == 0.0
    assert preview["proposed_used_amount"] == 986.0
    assert database.entlastung_year_balance.find_one()["remaining_amount"] == 131.0
    assert database.entlastung_replay_runs.count_documents({}) == 0


def test_apply_checkpoint_corrects_the_ledger_and_is_idempotent(monkeypatch):
    database = mongomock.MongoClient().test
    monkeypatch.setattr(mongodb_config, "_database", database)
    _reviewed_checkpoint_fixture(database)

    result = apply_checkpoint("rri_july", "reviewer@example.com")

    assert result["status"] == "applied"
    assert result["checkpoint_delta"] == 69.0
    assert result["before_remaining_amount"] == 131.0
    assert result["after_remaining_amount"] == 0.0

    row = database.entlastung_year_balance.find_one({"patient_id": "pat_synthetic"})
    assert row["remaining_amount"] == 0.0
    assert row["used_amount"] == 986.0
    assert row["accrued_amount"] == 917.0
    assert row["credited_through_month"] == "072026"

    item = database.rzh_reconciliation_items.find_one({"reconciliation_item_id": "rri_july"})
    assert item["apply_status"] == "applied"

    adjustment = database.entlastung_balance_adjustments.find_one({"reconciliation_item_id": "rri_july"})
    assert adjustment["amount_cents"] == 6900
    assert adjustment["adjustment_type"] == "rzh_checkpoint"
    assert database.entlastung_replay_runs.count_documents({"trigger_item_id": "rri_july"}) == 1
    assert database.care_event_history.count_documents({"action": "entlastung_ledger_checkpoint_applied"}) == 1

    repeated = apply_checkpoint("rri_july", "reviewer@example.com")
    assert repeated == {"status": "already_applied", "reconciliation_item_id": "rri_july"}
    assert database.entlastung_balance_adjustments.count_documents({}) == 1
    assert database.entlastung_replay_runs.count_documents({}) == 1


def test_apply_checkpoint_requires_a_confirmed_private_amount_first(monkeypatch):
    database = mongomock.MongoClient().test
    monkeypatch.setattr(mongodb_config, "_database", database)
    _reviewed_checkpoint_fixture(database)
    database.rzh_reconciliation_items.update_one({"reconciliation_item_id": "rri_july"},
        {"$set": {"apply_status": "not_applied"}})

    with pytest.raises(HTTPException, match="Confirm the private amount"):
        preview_checkpoint("rri_july")
    with pytest.raises(HTTPException, match="Confirm the private amount"):
        apply_checkpoint("rri_july", "reviewer@example.com")


def test_apply_checkpoint_requires_a_locked_opening_snapshot(monkeypatch):
    database = mongomock.MongoClient().test
    monkeypatch.setattr(mongodb_config, "_database", database)
    _reviewed_checkpoint_fixture(database)
    database.entlastung_opening_snapshots.delete_many({})

    with pytest.raises(HTTPException, match="opening snapshot"):
        apply_checkpoint("rri_july", "reviewer@example.com")


def test_apply_checkpoint_corrects_the_ledger_downward_when_ledger_was_too_generous(monkeypatch):
    """Regression test for the primary real-world case: RZH confirms a LOWER
    available amount than the replay estimated (checkpoint_delta negative).
    This is the exact scenario the feature exists for (an exhausted monthly
    maximum) and previously crashed with `ValueError: Amount must be finite
    and nonnegative` because the delta was routed through money(), which
    asserts nonnegative.
    """
    database = mongomock.MongoClient().test
    monkeypatch.setattr(mongodb_config, "_database", database)
    database.patient_profiles.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic",
        "patient_name": "Synthetic", "insurance_number": "SYNTHETIC"})
    database.entlastung_opening_snapshots.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic",
        "entitlement_year": 2026, "opening_as_of": "2026-06-30", "opening_locked": True,
        "opening_logic_version": "test", "opening_accrued_amount": 786.0, "opening_used_amount": 686.0,
        "opening_remaining_amount": 100.0, "historical_excess_usage": 0.0})
    database.care_events.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "care_event_id": "evt_july",
        "event_type": "Entleistung", "care_account": "4064", "invoicing_month": "072026", "sum_total": 212.25,
        "period_start_date": "01.07.2026", "period_end_date": "31.07.2026"})
    # Confirmed (Phase 2) covered amount is LOWER than what the replay would
    # otherwise estimate as available (100 opening + 131 July credit = 231).
    database.billing_details.insert_one({"org_id": "org_default", "billing_detail_id": "bill_july",
        "care_event_id": "evt_july", "invoicing_month": "072026", "sum_total": 212.25, "sum_covered": 131.0,
        "amount_owed": 81.25, "billing_status": "invoice_needed", "reconciliation_status": "confirmed_limit_partial",
        "confirmed_sum_covered": 131.0, "confirmed_amount_owed": 81.25, "reconciliation_item_id": "rri_july"})
    database.entlastung_year_balance.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic",
        "entitlement_year": 2026, "accrued_amount": 917.0, "used_amount": 686.0, "remaining_amount": 231.0,
        "credited_through_month": "072026"})
    database.rzh_reconciliation_items.insert_one({"org_id": "org_default", "reconciliation_item_id": "rri_july",
        "item_fingerprint": "synthetic-july", "reason_code": "ENTITLEMENT_EXHAUSTED", "amount_cents": -8125,
        "match_status": "matched", "matched_care_event_id": "evt_july", "apply_status": "reviewed"})

    preview = preview_checkpoint("rri_july")
    assert preview["checkpoint_delta"] == -100.0
    assert preview["proposed_remaining_amount"] == 0.0
    assert preview["proposed_used_amount"] == 817.0

    result = apply_checkpoint("rri_july", "reviewer@example.com")

    assert result["checkpoint_delta"] == -100.0
    assert result["before_remaining_amount"] == 231.0
    assert result["after_remaining_amount"] == 0.0
    assert result["before_used_amount"] == 686.0
    assert result["after_used_amount"] == 817.0

    row = database.entlastung_year_balance.find_one({"patient_id": "pat_synthetic"})
    assert row["remaining_amount"] == 0.0
    assert row["used_amount"] == 817.0

    adjustment = database.entlastung_balance_adjustments.find_one({"reconciliation_item_id": "rri_july"})
    assert adjustment["amount_cents"] == -10000


def test_dry_run_does_not_crash_when_confirmed_coverage_exceeds_the_live_total(monkeypatch):
    """Regression test: replay_patient_dry_run must not crash via money() when
    a confirmed bill's sum_covered exceeds the event's current sum_total (e.g.
    the event was edited down after confirmation). It should surface the
    inconsistency as a negative effective_amount_owed, not raise.
    """
    database = mongomock.MongoClient().test
    monkeypatch.setattr(mongodb_config, "_database", database)
    database.patient_profiles.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic",
        "patient_name": "Synthetic", "insurance_number": "SYNTHETIC"})
    database.entlastung_opening_snapshots.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic",
        "entitlement_year": 2026, "opening_as_of": "2026-06-30", "opening_locked": True,
        "opening_logic_version": "test", "opening_accrued_amount": 786.0, "opening_used_amount": 786.0,
        "opening_remaining_amount": 0.0, "historical_excess_usage": 0.0})
    database.care_events.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "care_event_id": "evt_july",
        "event_type": "Entleistung", "invoicing_month": "072026", "sum_total": 50.0,
        "period_start_date": "01.07.2026", "period_end_date": "31.07.2026"})
    database.billing_details.insert_one({"org_id": "org_default", "billing_detail_id": "bill_july",
        "care_event_id": "evt_july", "sum_covered": 131.0, "reconciliation_status": "confirmed_limit_full"})

    report = replay_patient_dry_run("pat_synthetic", "072026")

    assert report["events"][0]["effective_sum_covered"] == 131.0
    assert report["events"][0]["effective_amount_owed"] == -81.0


def test_apply_checkpoint_accounts_for_an_earlier_reviewed_but_unapplied_checkpoint(monkeypatch):
    """Regression test for out-of-order approval: applying a LATER event's
    checkpoint must fold in an EARLIER event's already-confirmed (Phase 2)
    amount even if that earlier item's own ledger checkpoint was never
    separately applied -- not silently fall back to the stale ledger estimate.
    """
    database = mongomock.MongoClient().test
    monkeypatch.setattr(mongodb_config, "_database", database)
    database.patient_profiles.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic",
        "patient_name": "Synthetic", "insurance_number": "SYNTHETIC"})
    database.entlastung_opening_snapshots.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic",
        "entitlement_year": 2026, "opening_as_of": "2026-06-30", "opening_locked": True,
        "opening_logic_version": "test", "opening_accrued_amount": 786.0, "opening_used_amount": 686.0,
        "opening_remaining_amount": 100.0, "historical_excess_usage": 0.0})
    database.care_events.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "care_event_id": "evt_july",
        "event_type": "Entleistung", "care_account": "4064", "invoicing_month": "072026", "sum_total": 212.25,
        "period_start_date": "01.07.2026", "period_end_date": "31.07.2026"})
    database.care_events.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "care_event_id": "evt_august",
        "event_type": "Entleistung", "care_account": "4064", "invoicing_month": "082026", "sum_total": 169.80,
        "period_start_date": "01.08.2026", "period_end_date": "31.08.2026"})
    # July is confirmed (Phase 2) but its own ledger checkpoint was never applied.
    database.billing_details.insert_one({"org_id": "org_default", "billing_detail_id": "bill_july",
        "care_event_id": "evt_july", "invoicing_month": "072026", "sum_total": 212.25, "sum_covered": 131.0,
        "amount_owed": 81.25, "reconciliation_status": "confirmed_limit_partial", "reconciliation_item_id": "rri_july"})
    database.billing_details.insert_one({"org_id": "org_default", "billing_detail_id": "bill_august",
        "care_event_id": "evt_august", "invoicing_month": "082026", "sum_total": 169.80, "sum_covered": 131.0,
        "amount_owed": 38.80, "reconciliation_status": "confirmed_limit_partial", "reconciliation_item_id": "rri_august"})
    database.entlastung_year_balance.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic",
        "entitlement_year": 2026, "accrued_amount": 917.0, "used_amount": 686.0, "remaining_amount": 231.0,
        "credited_through_month": "082026"})
    database.rzh_reconciliation_items.insert_one({"org_id": "org_default", "reconciliation_item_id": "rri_july",
        "item_fingerprint": "synthetic-july", "reason_code": "ENTITLEMENT_EXHAUSTED", "amount_cents": -8125,
        "match_status": "matched", "matched_care_event_id": "evt_july", "apply_status": "reviewed"})
    database.rzh_reconciliation_items.insert_one({"org_id": "org_default", "reconciliation_item_id": "rri_august",
        "item_fingerprint": "synthetic-august", "reason_code": "ENTITLEMENT_EXHAUSTED", "amount_cents": -3880,
        "match_status": "matched", "matched_care_event_id": "evt_august", "apply_status": "reviewed"})

    # If July's still-unapplied confirmation were ignored, the replay would
    # carry forward July's naive full-estimate balance (231 - 212.25 = 18.75),
    # credit August's 131, and compute a spurious August delta of -18.75.
    # Correctly folding July's confirmed 131 in first leaves nothing left to
    # correct for August.
    preview = preview_checkpoint("rri_august")

    assert preview["checkpoint_delta"] == 0.0
    assert preview["proposed_used_amount"] == 948.0
    assert preview["proposed_remaining_amount"] == 0.0


def test_reference_year_trajectory_rolls_unused_balance_forward_month_to_month(monkeypatch):
    """Sec.45b: unused monthly credit is never lost -- it rolls into next
    month alongside the new 131 credit. January unused -> February starts at
    262; February uses 160 -> March starts at (262 - 160) + 131 = 233.

    This must hold across the whole year uniformly, including January-June --
    unlike the operational ledger, this reference calculation is not gated by
    GO_LIVE_MONTH and never touches entlastung_year_balance or any billing row.
    """
    database = mongomock.MongoClient().test
    monkeypatch.setattr(mongodb_config, "_database", database)
    database.care_events.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "care_event_id": "evt_feb",
        "event_type": "Entleistung", "care_account": "4064", "invoicing_month": "022026",
        "period_start_date": "01.02.2026", "period_end_date": "28.02.2026", "sum_total": 160.0})
    database.care_events.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "care_event_id": "evt_march",
        "event_type": "Entleistung", "care_account": "4064", "invoicing_month": "032026",
        "period_start_date": "01.03.2026", "period_end_date": "31.03.2026", "sum_total": 50.0})

    trajectory = reference_year_trajectory("pat_synthetic", database)

    assert trajectory["evt_feb"]["available_before"] == 262.0
    assert trajectory["evt_feb"]["reference_covered"] == 160.0
    assert trajectory["evt_feb"]["balance_after"] == 102.0
    assert trajectory["evt_march"]["available_before"] == 233.0
    assert trajectory["evt_march"]["reference_covered"] == 50.0
    assert trajectory["evt_march"]["balance_after"] == 183.0
    assert database.entlastung_year_balance.count_documents({}) == 0
    assert database.billing_details.count_documents({}) == 0


def test_reference_year_trajectory_matches_a_non_string_care_account(monkeypatch):
    """Regression: care_account is not guaranteed to be stored as the literal
    string "4064" for every record (see the defensive str() cast used
    everywhere else this field is read, e.g. rzh_reconciliation.py::match_item).
    A strict Mongo-level equality filter on this field silently drops such
    events instead of erroring, which previously made the whole reference
    column render as empty for every row.
    """
    database = mongomock.MongoClient().test
    monkeypatch.setattr(mongodb_config, "_database", database)
    database.care_events.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "care_event_id": "evt_int_account",
        "event_type": "Entleistung", "care_account": 4064, "invoicing_month": "072026",
        "period_start_date": "01.07.2026", "period_end_date": "31.07.2026", "sum_total": 50.0})

    trajectory = reference_year_trajectory("pat_synthetic", database)

    assert "evt_int_account" in trajectory
    assert trajectory["evt_int_account"]["available_before"] == 7 * MONTHLY_CREDIT
