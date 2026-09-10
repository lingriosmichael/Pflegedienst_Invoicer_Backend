import mongomock
import pytest

from app import entlastung_balance as eb

ORG = "org_default"


@pytest.fixture
def db(monkeypatch):
    client = mongomock.MongoClient()
    database = client["test_db"]
    monkeypatch.setattr(eb, "get_database", lambda: database)
    return database


def _make_patient(db, patient_id):
    db.patient_profiles.insert_one({"org_id": ORG, "patient_id": patient_id, "patient_name": patient_id})


def _make_entleistung_event(db, patient_id, invoicing_month, sum_total):
    db.care_events.insert_one({
        "org_id": ORG,
        "patient_id": patient_id,
        "event_type": "Entleistung",
        "invoicing_month": invoicing_month,
        "sum_total": sum_total,
    })


def _seed_row(db, patient_id, year, credited_through_month, accrued, used):
    """Directly insert a balance row, bypassing ensure_credit's own creation semantics,
    so tests can isolate the incremental-credit / usage logic against a known starting state."""
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


# ----------------------------------------------------------------------
# uses_new_entlastung_logic
# ----------------------------------------------------------------------

@pytest.mark.parametrize("month,expected", [
    ("062026", False),
    ("072026", True),
    ("082026", True),
    ("092026", True),
    ("072025", False),
    ("082027", True),
    ("012027", True),
])
def test_uses_new_entlastung_logic_boundary(month, expected):
    assert eb.uses_new_entlastung_logic(month) is expected


# ----------------------------------------------------------------------
# ensure_entlastung_credit_through
# ----------------------------------------------------------------------

def test_ensure_credit_on_fresh_row_backfills_from_january(db):
    """A row with no prior credit history accrues retroactively from Jan of its year,
    same principle as seeding: nothing tracks it yet, so it's owed since day one."""
    row = eb.ensure_entlastung_credit_through("pat_1", "082026")
    assert row["credited_through_month"] == "082026"
    assert row["accrued_amount"] == pytest.approx(131.00 * 8)
    assert row["remaining_amount"] == pytest.approx(131.00 * 8)
    assert row["used_amount"] == 0
    assert row["entitlement_year"] == 2026


def test_ensure_credit_is_idempotent_within_same_month(db):
    _seed_row(db, "pat_1", 2026, "072026", accrued=917.0, used=0.0)
    row = eb.ensure_entlastung_credit_through("pat_1", "072026")
    assert row["accrued_amount"] == pytest.approx(917.0)


def test_ensure_credit_catches_up_skipped_months(db):
    _seed_row(db, "pat_1", 2026, "072026", accrued=917.0, used=0.0)
    row = eb.ensure_entlastung_credit_through("pat_1", "102026")  # app first opened in October
    # August, September, October = 3 missing months
    assert row["credited_through_month"] == "102026"
    assert row["accrued_amount"] == pytest.approx(917.0 + 131.00 * 3)
    assert row["remaining_amount"] == pytest.approx(917.0 + 131.00 * 3)


def test_ensure_credit_never_double_adds_on_repeated_startup(db):
    _seed_row(db, "pat_1", 2026, "072026", accrued=917.0, used=0.0)
    eb.ensure_entlastung_credit_through("pat_1", "082026")
    eb.ensure_entlastung_credit_through("pat_1", "082026")
    eb.ensure_entlastung_credit_through("pat_1", "082026")
    row = eb.get_balance("pat_1", 2026)
    assert row["accrued_amount"] == pytest.approx(917.0 + 131.00)


# ----------------------------------------------------------------------
# apply_entlastung_usage
# ----------------------------------------------------------------------

def test_usage_fully_covered_when_under_balance(db):
    _seed_row(db, "pat_1", 2026, "082026", accrued=1048.0, used=0.0)
    result = eb.apply_entlastung_usage("pat_1", "082026", 90.0)
    assert result == {"covered": 90.0, "owed": 0.0}
    row = eb.get_balance("pat_1", 2026)
    assert row["used_amount"] == pytest.approx(90.0)
    assert row["remaining_amount"] == pytest.approx(1048.0 - 90.0)


def test_usage_partially_covered_when_exceeding_balance(db):
    _seed_row(db, "pat_1", 2026, "082026", accrued=131.0, used=0.0)
    result = eb.apply_entlastung_usage("pat_1", "082026", 200.0)
    assert result["covered"] == pytest.approx(131.00)
    assert result["owed"] == pytest.approx(69.00)
    row = eb.get_balance("pat_1", 2026)
    assert row["remaining_amount"] == pytest.approx(0.0)


def test_usage_across_two_months_consumes_cumulative_balance(db):
    _seed_row(db, "pat_1", 2026, "072026", accrued=917.0, used=0.0)
    eb.apply_entlastung_usage("pat_1", "082026", 100.0)
    result = eb.apply_entlastung_usage("pat_1", "092026", 100.0)
    assert result == {"covered": 100.0, "owed": 0.0}
    row = eb.get_balance("pat_1", 2026)
    # 917 + Aug(131) + Sep(131) accrued, minus 200 used
    assert row["remaining_amount"] == pytest.approx(917.0 + 131.0 * 2 - 200.0)
    assert row["used_amount"] == pytest.approx(200.0)


def test_reverse_and_recompute_usage(db):
    _seed_row(db, "pat_1", 2026, "082026", accrued=131.0, used=0.0)
    eb.apply_entlastung_usage("pat_1", "082026", 100.0)
    result = eb.recompute_entlastung_for_care_event("pat_1", "082026", new_sum_total=50.0, previously_covered=100.0)
    assert result == {"covered": 50.0, "owed": 0.0}
    row = eb.get_balance("pat_1", 2026)
    assert row["used_amount"] == pytest.approx(50.0)
    assert row["remaining_amount"] == pytest.approx(131.0 - 50.0)


# ----------------------------------------------------------------------
# seed_entlastung_2026_balances
# ----------------------------------------------------------------------

def test_seed_patient_with_no_usage(db):
    _make_patient(db, "pat_1")
    summary = eb.seed_entlastung_2026_balances()
    assert summary == {"seeded": 1, "skipped_existing": 0, "total_patients": 1}
    row = eb.get_balance("pat_1", 2026)
    assert row["accrued_amount"] == pytest.approx(131.0 * 6)
    assert row["used_amount"] == 0
    assert row["remaining_amount"] == pytest.approx(131.0 * 6)
    assert row["credited_through_month"] == "062026"


def test_seed_patient_with_partial_usage(db):
    _make_patient(db, "pat_1")
    _make_entleistung_event(db, "pat_1", "032026", 100.0)
    _make_entleistung_event(db, "pat_1", "052026", 127.35)
    eb.seed_entlastung_2026_balances()
    row = eb.get_balance("pat_1", 2026)
    expected_used = 100.0 + 127.35
    assert row["used_amount"] == pytest.approx(expected_used)
    assert row["remaining_amount"] == pytest.approx(131.0 * 6 - expected_used)


def test_seed_caps_each_historical_event_at_legacy_cap(db):
    _make_patient(db, "pat_1")
    _make_entleistung_event(db, "pat_1", "032026", 500.0)  # way over the old 127.35 cap
    eb.seed_entlastung_2026_balances()
    row = eb.get_balance("pat_1", 2026)
    assert row["used_amount"] == pytest.approx(eb.LEGACY_COVERAGE_CAP)


def test_seed_floors_remaining_at_zero_when_usage_exceeds_accrual(db):
    _make_patient(db, "pat_1")
    # two Entleistung events in the same month push monthly usage above the 131 monthly credit
    for month in ["012026", "022026", "032026", "042026", "052026", "062026"]:
        _make_entleistung_event(db, "pat_1", month, 127.35)
    _make_entleistung_event(db, "pat_1", "062026", 127.35)  # second event in June
    eb.seed_entlastung_2026_balances()
    row = eb.get_balance("pat_1", 2026)
    assert row["remaining_amount"] == 0.0
    # July should still add exactly one more month's credit on top of the floored balance
    updated = eb.ensure_entlastung_credit_through("pat_1", "072026")
    assert updated["remaining_amount"] == pytest.approx(131.0)


def test_seed_ignores_events_outside_org_or_wrong_type(db):
    _make_patient(db, "pat_1")
    db.care_events.insert_one({
        "org_id": ORG, "patient_id": "pat_1", "event_type": "SGBXI",
        "invoicing_month": "032026", "sum_total": 999.0,
    })
    eb.seed_entlastung_2026_balances()
    row = eb.get_balance("pat_1", 2026)
    assert row["used_amount"] == 0.0


def test_seed_is_idempotent_and_never_overwrites_existing_row(db):
    _make_patient(db, "pat_1")
    eb.seed_entlastung_2026_balances()
    eb.apply_entlastung_usage("pat_1", "082026", 50.0)  # mutate the row after seeding
    summary = eb.seed_entlastung_2026_balances()
    assert summary == {"seeded": 0, "skipped_existing": 1, "total_patients": 1}
    row = eb.get_balance("pat_1", 2026)
    assert row["used_amount"] == pytest.approx(50.0)  # untouched by the second seed call


def test_seed_multiple_patients_independent_balances(db):
    _make_patient(db, "pat_1")
    _make_patient(db, "pat_2")
    _make_entleistung_event(db, "pat_1", "012026", 100.0)
    summary = eb.seed_entlastung_2026_balances()
    assert summary["seeded"] == 2
    assert eb.get_balance("pat_1", 2026)["used_amount"] == pytest.approx(100.0)
    assert eb.get_balance("pat_2", 2026)["used_amount"] == 0.0
