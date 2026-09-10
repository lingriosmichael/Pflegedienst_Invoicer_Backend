from datetime import datetime, timezone

import mongomock
import pytest

from app import database


ORG = "org_default"


@pytest.fixture
def db(monkeypatch):
    result = mongomock.MongoClient()["test_db"]
    monkeypatch.setattr(database, "get_database", lambda: result)
    return result


def _event(event_id, event_type, created_at):
    return {
        "org_id": ORG,
        "care_event_id": event_id,
        "patient_id": "pat_test",
        "event_type": event_type,
        "invoicing_month": "052026",
        "period_start_date": "01.05.2026",
        "period_end_date": "28.05.2026",
        "sum_total": 100.0,
        "sum_covered": 100.0,
        "created_at": created_at,
    }


def test_sgbv_and_verhinderungspflege_receive_not_needed_markers(db):
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    db.care_events.insert_many([
        _event("evt_sgbv", "SGBV", now),
        _event("evt_verh", "Verhinderungspflege", now),
    ])

    database.mark_month_ready_for_generation("052026", event_types=["SGBV", "Verhinderungspflege"])

    assert {bill["billing_status"] for bill in db.billing_details.find({})} == {"not_needed"}


def test_unchanged_entlastung_closes_after_four_calendar_months(db):
    imported_at = datetime(2026, 5, 10, tzinfo=timezone.utc)
    db.care_events.insert_one(_event("evt_ent", "Entleistung", imported_at))
    database.mark_month_ready_for_generation("052026", event_types=["Entleistung"])

    changed = database.expire_stale_entlastung_coverage(datetime(2026, 9, 10, tzinfo=timezone.utc))

    assert changed == 1
    assert db.billing_details.find_one({"care_event_id": "evt_ent"})["billing_status"] == "not_needed"


def test_rzh_correction_keeps_entlastung_open_for_review(db):
    imported_at = datetime(2026, 5, 10, tzinfo=timezone.utc)
    db.care_events.insert_one(_event("evt_ent", "Entleistung", imported_at))
    database.mark_month_ready_for_generation("052026", event_types=["Entleistung"])
    db.rzh_reconciliation_items.insert_one({
        "org_id": ORG,
        "matched_care_event_id": "evt_ent",
        "section_type": "absetzung",
        "amount_cents": -1000,
    })

    changed = database.expire_stale_entlastung_coverage(datetime(2026, 9, 10, tzinfo=timezone.utc))

    assert changed == 0
    assert db.billing_details.find_one({"care_event_id": "evt_ent"})["billing_status"] == "covered_insurance"
