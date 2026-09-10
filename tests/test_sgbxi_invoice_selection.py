import mongomock
import pytest

from app import database
from app.routers import invoicing


ORG = "org_default"


@pytest.fixture
def db(monkeypatch):
    result = mongomock.MongoClient()["test_db"]
    monkeypatch.setattr(database, "get_database", lambda: result)
    monkeypatch.setattr(invoicing, "get_database", lambda: result)
    return result


def _event(event_id, event_type, patient_id, total):
    return {
        "org_id": ORG,
        "care_event_id": event_id,
        "event_type": event_type,
        "patient_id": patient_id,
        "invoicing_month": "082026",
        "period_start_date": "01.08.2026",
        "period_end_date": "28.08.2026",
        "sum_total": total,
        "sum_covered": total,
    }


def test_sgbxi_prepare_lists_only_sgbxi_and_not_entlastung(db, monkeypatch):
    db.patient_profiles.insert_one({"org_id": ORG, "patient_id": "pat_1", "patient_name": "Test Patient"})
    db.care_events.insert_many([
        _event("evt_sgbxi", "SGBXI", "pat_1", 100.0),
        _event("evt_ent", "Entleistung", "pat_1", 200.0),
    ])
    monkeypatch.setattr(invoicing, "ensure_service_packets", lambda month: None)

    response = invoicing.prepare_sgbxi_invoices(invoicing.InvoiceRequest(abrechnungsmonat="082026"))

    assert response["created"] == 1
    assert [candidate["care_event_id"] for candidate in response["candidates"]] == ["evt_sgbxi"]
    assert db.billing_details.count_documents({"care_event_id": "evt_ent"}) == 0


def test_sgbxi_issue_all_issues_only_current_sgbxi_candidates(db, monkeypatch):
    db.patient_profiles.insert_one({"org_id": ORG, "patient_id": "pat_1", "patient_name": "Test Patient"})
    db.care_events.insert_one(_event("evt_sgbxi", "SGBXI", "pat_1", 100.0))
    monkeypatch.setattr(invoicing, "ensure_service_packets", lambda month: None)
    prepared = invoicing.prepare_sgbxi_invoices(invoicing.InvoiceRequest(abrechnungsmonat="082026"))
    selected_id = prepared["candidates"][0]["billing_detail_id"]
    captured = {}

    def fake_generate(month, **kwargs):
        captured.update({"month": month, **kwargs})
        return {"total_cases": 1, "generated": 1, "failed": 0, "failed_invoice_ids": []}

    monkeypatch.setattr(invoicing.invoice_generator, "process_generate_invoices", fake_generate)
    response = invoicing.issue_all_sgbxi_invoices(invoicing.InvoiceRequest(abrechnungsmonat="082026"))

    assert response["generated"] == 1
    assert captured["billing_detail_ids"] == [selected_id]
    assert captured["allowed_event_types"] == {"SGBXI", "ServicePacket"}
