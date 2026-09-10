import mongomock
import pytest

from app import database
from app import entlastung_balance as eb

ORG = "org_default"


@pytest.fixture
def db(monkeypatch):
    client = mongomock.MongoClient()
    database_ = client["test_db"]
    monkeypatch.setattr(database, "get_database", lambda: database_)
    monkeypatch.setattr(eb, "get_database", lambda: database_)
    return database_


def _make_care_event(db, patient_id, invoicing_month, sum_total, period_start_date=None, period_end_date=None):
    care_event_id = f"evt_{patient_id}_{invoicing_month}"
    db.care_events.insert_one({
        "org_id": ORG,
        "care_event_id": care_event_id,
        "patient_id": patient_id,
        "event_type": "Entleistung",
        "invoicing_month": invoicing_month,
        "sum_total": sum_total,
        "period_start_date": period_start_date or f"01.{invoicing_month[:2]}.{invoicing_month[2:]}",
        "period_end_date": period_end_date or f"28.{invoicing_month[:2]}.{invoicing_month[2:]}",
    })
    return care_event_id


def _seed_row(db, patient_id, year, credited_through_month, accrued, used):
    db[eb.COLLECTION].insert_one({
        "org_id": ORG,
        "patient_id": patient_id,
        "entitlement_year": year,
        "credited_through_month": credited_through_month,
        "accrued_amount": accrued,
        "used_amount": used,
        "remaining_amount": round(accrued - used, 2),
        "manual_adjustment": 0.0,
        "manual_adjustment_note": None,
        "expires_on": eb._expires_on_for_year(year),
        "logic_version": eb.LOGIC_VERSION,
    })


def test_entlastung_is_presumed_covered_and_does_not_consume_reference_balance(db):
    _seed_row(db, "pat_1", 2026, "072026", accrued=917.0, used=0.0)
    _make_care_event(db, "pat_1", "082026", sum_total=90.0)

    created = database.mark_month_ready_for_generation("082026")

    assert created == 1
    bd = db.billing_details.find_one({"org_id": ORG})
    assert bd["billing_status"] == "covered_insurance"
    assert bd["sum_covered"] == pytest.approx(90.0)
    assert bd["amount_owed"] == pytest.approx(0.0)
    balance = eb.get_balance("pat_1", 2026)
    assert balance["used_amount"] == pytest.approx(0.0)
    assert bd["reconciliation_status"] == "assumed_covered_until_rzh"
    assert bd["coverage_source"] == "assumed_full_until_rzh"


def test_entlastung_never_becomes_invoice_needed_from_allowance_reference(db):
    _seed_row(db, "pat_1", 2026, "072026", accrued=917.0, used=850.0)
    _make_care_event(db, "pat_1", "082026", sum_total=200.0)

    database.mark_month_ready_for_generation("082026")

    bd = db.billing_details.find_one({"org_id": ORG})
    assert bd["billing_status"] == "covered_insurance"
    assert bd["sum_covered"] == pytest.approx(200.0)
    assert bd["amount_owed"] == pytest.approx(0.0)
    assert eb.get_balance("pat_1", 2026)["used_amount"] == pytest.approx(850.0)


def test_entlastung_is_presumed_covered_regardless_of_month_or_reference_balance(db):
    _seed_row(db, "pat_1", 2026, "072026", accrued=917.0, used=850.0)
    _make_care_event(db, "pat_1", "082026", sum_total=200.0)

    database.mark_month_ready_for_generation("082026")

    bill = db.billing_details.find_one({"org_id": ORG})
    assert bill["billing_status"] == "covered_insurance"
    assert bill["reconciliation_status"] == "assumed_covered_until_rzh"
    assert bill["sum_covered"] == pytest.approx(200.0)


def test_rerunning_entlastung_marker_does_not_change_reference_balance(db):
    _seed_row(db, "pat_1", 2026, "072026", accrued=917.0, used=0.0)
    _make_care_event(db, "pat_1", "082026", sum_total=90.0)

    database.mark_month_ready_for_generation("082026")
    created_second_run = database.mark_month_ready_for_generation("082026")

    assert created_second_run == 0  # already has billing_details, skipped
    balance = eb.get_balance("pat_1", 2026)
    assert balance["used_amount"] == pytest.approx(0.0)
    assert db.billing_details.count_documents({"org_id": ORG}) == 1


def test_historic_entlastung_is_also_presumed_covered_until_rzh_confirms(db):
    _make_care_event(db, "pat_1", "062026", sum_total=200.0)

    database.mark_month_ready_for_generation("062026")

    bd = db.billing_details.find_one({"org_id": ORG})
    assert bd["sum_covered"] == pytest.approx(200.0)
    assert bd["amount_owed"] == pytest.approx(0.0)
    assert bd["billing_status"] == "covered_insurance"
    # historic months also never touch the reference balance collection
    assert eb.get_balance("pat_1", 2026) is None
