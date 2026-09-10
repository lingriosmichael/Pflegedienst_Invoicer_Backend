import copy
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from pymongo import MongoClient
from pymongo.collection import Collection

from app import database, entlastung_balance
from app.db import mongodb_config
from app.db.migrations import migrate
from app.billing_operations import update_services
from app.routers.billing import ServicesUpdateRequest
from app.import_records import persist_record
from app.invoice_issuance import ensure_service_packets, prepare_invoice
from app.invoice_generator import generate_billing_pdf, process_generate_invoices
from app.deletion import delete_month_records
from app.rzh_reconciliation import register_batch


pytestmark = pytest.mark.mongo
ORG = "org_default"


@pytest.fixture
def mongo(monkeypatch):
    uri = os.getenv("TEST_MONGODB_URI")
    if not uri:
        pytest.skip("TEST_MONGODB_URI is required for replica-set integration tests")
    client = MongoClient(uri, directConnection=True, serverSelectionTimeoutMS=3000)
    name = "review_test_" + uuid.uuid4().hex
    isolated = client[name]
    monkeypatch.setattr(mongodb_config, "_database", isolated)
    monkeypatch.setattr(mongodb_config, "_client", client)
    migrate(apply=True)
    yield isolated
    client.drop_database(name)
    client.close()


def seed(mongo, remaining=100.0, month="072026"):
    mongo.patient_profiles.insert_one({"org_id": ORG, "patient_id": "synthetic", "patient_name": "Synthetic Review",
        "insurance_number": "SYNTHETIC", "include_service_packet": False})
    mongo.entlastung_year_balance.insert_one({"org_id": ORG, "patient_id": "synthetic", "entitlement_year": 2026,
        "credited_through_month": month, "accrued_amount": 917.0, "used_amount": 917.0 - remaining,
        "remaining_amount": remaining})


def event(mongo, identifier="evt_synthetic", total=80.0, month="072026", event_type="Entleistung"):
    mongo.care_events.insert_one({"org_id": ORG, "patient_id": "synthetic", "care_event_id": identifier,
        "event_type": event_type, "invoicing_month": month, "sum_total": total, "sum_covered": 0.0,
        "period_start_date": f"01.{month[:2]}.{month[2:]}", "period_end_date": f"28.{month[:2]}.{month[2:]}",
        "services": [{"service_code": "test", "service_description": "Synthetic", "quantity_value": 1.0,
                      "unit_price": total, "line_total": total}]})


def test_failed_marker_rolls_back_credit_and_retry_is_covered(mongo, monkeypatch):
    seed(mongo)
    event(mongo)
    original = Collection.insert_one

    def fail_marker(collection, document, *args, **kwargs):
        if collection.name == "billing_details":
            raise RuntimeError("Injected marker failure")
        return original(collection, document, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(Collection, "insert_one", fail_marker)
        with pytest.raises(RuntimeError):
            database.mark_month_ready_for_generation("072026")
    assert mongo.entlastung_year_balance.find_one()["remaining_amount"] == 100.0
    database.mark_month_ready_for_generation("072026")
    assert mongo.billing_details.find_one()["amount_owed"] == 0.0
    assert mongo.entlastung_year_balance.find_one()["remaining_amount"] == 20.0


def test_concurrent_readiness_has_one_allocation_and_one_marker(mongo):
    seed(mongo)
    event(mongo)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda unused: database.mark_month_ready_for_generation("072026"), range(4)))
    assert sum(results) == 1
    assert mongo.billing_details.count_documents({}) == 1
    assert mongo.entlastung_year_balance.find_one()["remaining_amount"] == 20.0


def test_concurrent_monthly_credit_posts_once(mongo):
    seed(mongo)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda unused: entlastung_balance.ensure_entlastung_credit_through("synthetic", "082026"), range(4)))
    row = mongo.entlastung_year_balance.find_one()
    assert row["accrued_amount"] == 1048.0
    assert row["remaining_amount"] == 231.0


def test_concurrent_events_cannot_overallocate(mongo):
    seed(mongo)
    with ThreadPoolExecutor(max_workers=2) as executor:
        allocations = list(executor.map(lambda unused: entlastung_balance.apply_entlastung_usage("synthetic", "072026", 80), range(2)))
    assert sum(allocation["covered"] for allocation in allocations) == 100.0
    assert mongo.entlastung_year_balance.find_one()["remaining_amount"] == 0.0


def test_edit_recomputes_status_and_rolls_back_on_failure(mongo, monkeypatch):
    seed(mongo)
    event(mongo)
    database.mark_month_ready_for_generation("072026")
    bill = mongo.billing_details.find_one()
    request = ServicesUpdateRequest(services=[{"service_code": "test", "service_description": "Synthetic",
        "quantity_value": 1, "unit_price": 150}])
    original = Collection.update_one

    def fail_bill(collection, *args, **kwargs):
        if collection.name == "billing_details":
            raise RuntimeError("Injected update failure")
        return original(collection, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(Collection, "update_one", fail_bill)
        with pytest.raises(RuntimeError):
            update_services(bill["billing_detail_id"], request)
    assert mongo.entlastung_year_balance.find_one()["remaining_amount"] == 20.0
    assert mongo.care_events.find_one()["sum_total"] == 80.0
    update_services(bill["billing_detail_id"], request)
    edited = mongo.billing_details.find_one()
    assert edited["billing_status"] == "invoice_needed"
    assert edited["amount_owed"] == 50.0


def test_late_patient_seed_uses_historical_events(mongo):
    event(mongo, total=200, month="032026")
    row = entlastung_balance.ensure_entlastung_credit_through("synthetic", "072026")
    assert row["used_amount"] == 127.35
    assert row["remaining_amount"] == 789.65


def record():
    return {"patient": {"name": "Synthetic Review", "birthdate": "01.01.1950", "insurance_number": "SYNTHETIC",
                        "care_level": "1", "pflege_konto": "4064"},
        "invoice": {"pflegezeitraum_beginn": "01.07.2026", "pflegezeitraum_ende": "31.07.2026",
                    "summe_total": "100,00", "summe_covered": "100,00"},
        "services": [{"code": "test", "description": "Synthetic", "quantity": "1", "unit_price": "100,00", "total_price": "100,00"}]}


def test_import_is_transactional_and_deduplicates(mongo, monkeypatch):
    first = persist_record(record(), "same-record", "072026")
    duplicate = persist_record(record(), "same-record", "072026")
    assert not first["duplicate"] and duplicate["duplicate"]
    assert first["care_event_id"] == duplicate["care_event_id"]
    assert mongo.care_events.count_documents({}) == 1
    original = Collection.insert_one

    def fail_history(collection, *args, **kwargs):
        if collection.name == "care_event_history":
            raise RuntimeError("Injected history failure")
        return original(collection, *args, **kwargs)

    monkeypatch.setattr(Collection, "insert_one", fail_history)
    with pytest.raises(RuntimeError):
        persist_record(record(), "another-record", "072026")
    assert mongo.care_events.count_documents({}) == 1


def test_packet_persists_once_and_concurrent_issuance_reuses_number(mongo):
    seed(mongo)
    mongo.patient_profiles.update_one({}, {"$set": {"include_service_packet": True}})
    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(lambda unused: ensure_service_packets("072026"), range(3)))
    assert mongo.service_packet_charges.count_documents({}) == 1
    assert mongo.billing_details.count_documents({}) == 1
    bill = mongo.billing_details.find_one()
    with ThreadPoolExecutor(max_workers=3) as executor:
        cases = list(executor.map(lambda unused: prepare_invoice(bill["billing_detail_id"]), range(3)))
    assert len({case["invoice"]["invoice_number"] for case in cases}) == 1
    assert mongo.invoice_sequences.find_one({"org_id": ORG})["last_number"] == 1


def test_multiple_sgbxi_events_receive_one_monthly_packet(mongo):
    seed(mongo)
    mongo.patient_profiles.update_one({}, {"$set": {"include_service_packet": True}})
    event(mongo, "evt_one", event_type="SGBXI")
    event(mongo, "evt_two", event_type="SGBXI")
    database.mark_month_ready_for_generation("072026")
    ensure_service_packets("072026")
    cases = [prepare_invoice(bill["billing_detail_id"]) for bill in mongo.billing_details.find({})]
    assert sum(case["patient"]["include_service_packet"] for case in cases) == 1


def test_entlastung_invoice_bypasses_service_packet_reconciliation(mongo):
    """A one-off 4064 invoice must not mutate or validate the SGB XI packet month."""
    seed(mongo)
    mongo.patient_profiles.update_one({}, {"$set": {"include_service_packet": True}})
    event(mongo, "evt_entlastung", total=200.0, event_type="Entleistung")
    database.mark_month_ready_for_generation("072026")
    entlastung_bill = mongo.billing_details.find_one({"care_event_id": "evt_entlastung"})

    # Simulate an older issued SGB XI invoice whose legacy packet marker is
    # absent. This is the state that previously blocked the 4064 invoice.
    event(mongo, "evt_issued_sgbxi", event_type="SGBXI")
    mongo.billing_details.insert_one({"org_id": ORG, "billing_detail_id": "bill_issued_sgbxi",
        "care_event_id": "evt_issued_sgbxi", "invoicing_month": "072026", "sum_total": 80.0,
        "sum_covered": 0.0, "amount_owed": 84.8, "invoice_number": 99,
        "billing_status": "sent"})

    case = prepare_invoice(entlastung_bill["billing_detail_id"])

    assert case["invoice"]["event_type"] == "Entleistung"
    assert mongo.service_packet_charges.count_documents({}) == 0


def test_delete_unissued_month_restores_usage_and_preserves_other_org(mongo):
    seed(mongo)
    event(mongo)
    database.mark_month_ready_for_generation("072026")
    mongo.care_events.insert_one({"org_id": "other", "patient_id": "other", "care_event_id": "evt_other",
        "event_type": "SGBXI", "invoicing_month": "072026"})
    delete_month_records("072026", dry_run=False)
    assert mongo.entlastung_year_balance.find_one()["remaining_amount"] == 100
    assert mongo.care_events.count_documents({"org_id": "other"}) == 1


def test_delete_issued_month_is_rejected(mongo):
    seed(mongo)
    event(mongo, event_type="SGBXI")
    database.mark_month_ready_for_generation("072026")
    prepare_invoice(mongo.billing_details.find_one()["billing_detail_id"])
    with pytest.raises(HTTPException, match="Issued"):
        delete_month_records("072026", dry_run=False)
    assert mongo.billing_details.count_documents({}) == 1


def test_awaiting_reconciliation_cannot_issue_or_regenerate(mongo):
    seed(mongo)
    event(mongo, total=200.0)
    database.mark_month_ready_for_generation("072026")
    bill = mongo.billing_details.find_one()
    mongo.billing_details.update_one({"_id": bill["_id"]}, {"$set": {"billing_status": "awaiting_reconciliation"}})

    with pytest.raises(HTTPException, match="awaiting RZH"):
        prepare_invoice(bill["billing_detail_id"])
    with pytest.raises(HTTPException, match="awaiting RZH"):
        generate_billing_pdf(bill["billing_detail_id"])

    regenerated = process_generate_invoices("072026", include_orphaned=False, require_invoice_needed=False)
    assert regenerated["total_cases"] == 0
    assert mongo.invoice_sequences.count_documents({}) == 0


def test_confirmation_flag_blocks_unconfirmed_entlastung_before_number_allocation(mongo, monkeypatch):
    seed(mongo)
    event(mongo, total=200.0)
    database.mark_month_ready_for_generation("072026")
    bill = mongo.billing_details.find_one()
    monkeypatch.setenv("ENTLASTUNG_REQUIRE_RZH_CONFIRMATION_FOR_PRIVATE_INVOICE", "true")

    with pytest.raises(HTTPException, match="requires confirmed RZH"):
        prepare_invoice(bill["billing_detail_id"])
    assert mongo.invoice_sequences.count_documents({}) == 0


def test_source_statement_registration_is_parser_version_independent(mongo):
    first = register_batch("synthetic.pdf", "synthetic-hash", "parser-v1")
    repeated = register_batch("synthetic.pdf", "synthetic-hash", "parser-v2")

    assert not first["already_registered"]
    assert repeated["already_registered"]
    assert mongo.rzh_reconciliation_batches.count_documents({}) == 1
    assert mongo.rzh_reconciliation_batches.find_one()["parser_versions"] == ["parser-v1", "parser-v2"]
