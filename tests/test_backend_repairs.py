import copy
import json
from pathlib import Path

import fitz
import mongomock
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import database, invoice_generator, openai_client, pdf_parser
from app.db import mongodb_config
from app.routers import analytics, billing, imports, patients
from app.utils.data_validation import DataNormalizer
from app.utils.validation import validate_month


@pytest.fixture
def isolated(monkeypatch):
    mock_database = mongomock.MongoClient().review
    monkeypatch.setattr(mongodb_config, "_database", mock_database)
    return mock_database


@pytest.mark.parametrize("value", ["not-an-amount", "NaN", "Infinity", "-1,00", ""])
def test_bad_amounts_are_rejected(value):
    with pytest.raises(ValueError):
        DataNormalizer.normalize_amount(value)


def test_patient_name_normalization_only_changes_all_caps_source_names():
    assert DataNormalizer.normalize_patient_name("SCHLAGEHAHN, BIANCA-MARIA") == "Schlagehahn, Bianca-Maria"
    assert DataNormalizer.normalize_patient_name("Fuchs, Peter") == "Fuchs, Peter"
    assert DataNormalizer.normalize_patient_name("VON DER HEIDE, ANNA") == "von der Heide, Anna"


@pytest.mark.parametrize("month", ["132026", "002026", "72026", "072026/", "0720000"])
def test_impossible_months_are_rejected(month):
    with pytest.raises(ValueError):
        validate_month(month)


def test_negative_service_update_is_rejected():
    with pytest.raises(ValidationError):
        billing.ServicesUpdateRequest(services=[{"service_code": "test", "service_description": "Synthetic",
            "quantity_value": -1, "unit_price": 10}])


def test_prepare_rejects_outside_paths_and_symlinks(tmp_path, monkeypatch):
    upload = tmp_path / "uploads"
    upload.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-synthetic")
    (upload / "link.pdf").symlink_to(outside)
    monkeypatch.setattr(imports, "UPLOAD_DIR", upload)
    for filename in (str(outside), "../outside.pdf", "link.pdf"):
        with pytest.raises(HTTPException):
            imports.resolve_pdf(filename)


def test_prepare_jobs_are_file_bound_and_summaries_idempotent(isolated, tmp_path, monkeypatch):
    monkeypatch.setattr(imports, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(pdf_parser, "extract_text_from_pdf", lambda path: Path(path).read_text())
    monkeypatch.setattr(pdf_parser, "extract_first_page_text", lambda path: "synthetic")
    monkeypatch.setattr(pdf_parser, "extract_billing_summary", lambda text: {"submitted_invoices_count": 1, "submitted_invoices_amount": 100.0})
    monkeypatch.setattr(pdf_parser, "split_into_chunks", lambda text: [text])
    (tmp_path / "a.pdf").write_text("synthetic A")
    (tmp_path / "b.pdf").write_text("synthetic B")
    first = imports.prepare_import("a.pdf", "072026")
    second = imports.prepare_import("b.pdf", "072026")
    imports.prepare_import("a.pdf", "072026")
    assert first["import_id"] != second["import_id"]
    assert imports.load_chunks(first)[0]["text"] == "synthetic A"
    assert isolated.billing_summary.find_one()["submitted_invoices_count"] == 2
    assert isolated.import_jobs.count_documents({}) == 2


def test_missing_summary_is_not_fabricated(isolated, tmp_path, monkeypatch):
    monkeypatch.setattr(imports, "UPLOAD_DIR", tmp_path)
    (tmp_path / "a.pdf").write_bytes(b"%PDF-synthetic")
    monkeypatch.setattr(pdf_parser, "extract_text_from_pdf", lambda path: "")
    monkeypatch.setattr(pdf_parser, "extract_first_page_text", lambda path: "")
    monkeypatch.setattr(pdf_parser, "extract_billing_summary", lambda text: None)
    imports.prepare_import("a.pdf", "072026")
    assert isolated.billing_summary.count_documents({}) == 0


def test_batch_mapping_uses_ids_instead_of_position(monkeypatch):
    monkeypatch.setattr(openai_client, "_extract", lambda *args: {"results": [
        {"chunk_id": "1", "record": {"synthetic": "second"}}]})
    assert openai_client.extract_batch_structured_data(["first", "second"]) == [None, {"synthetic": "second"}]


def test_failed_import_count_and_individual_retry(monkeypatch):
    from app import import_records

    monkeypatch.setattr(pdf_parser, "extract_batch_structured_data", lambda *args, **kwargs: [None, {"synthetic": 2}])
    monkeypatch.setattr(pdf_parser, "extract_structured_data_with_openai", lambda *args, **kwargs: {"synthetic": 1})

    def persist(record, *args):
        if record["synthetic"] == 1:
            return {"duplicate": False}
        raise ValueError("Synthetic failure")

    monkeypatch.setattr(import_records, "persist_record", persist)
    result = pdf_parser.process_import_sgbxi([{"chunk_id": "first", "text": "first"}, {"chunk_id": "second", "text": "second"}], "072026")
    assert (result["attempted"], result["committed"], result["failed"]) == (2, 1, 1)
    assert result["failed_ids"] == ["second"]


def test_entlastung_import_retries_unresolved_rzh_matches_without_masking_import(monkeypatch, isolated):
    from app.routers import imports
    from app import rzh_reconciliation

    monkeypatch.setattr(imports, "load_chunks", lambda job: [{"chunk_id": "synthetic", "text": "synthetic"}])
    monkeypatch.setattr(pdf_parser, "filter_chunks_by_mode", lambda chunks, mode: chunks)
    monkeypatch.setattr(pdf_parser, "process_import_sgbxi", lambda chunks, month, engine="llm": {
        "attempted": 1, "committed": 1, "duplicates": 0, "failed": 0, "failed_ids": [], "failures": []})
    monkeypatch.setattr(rzh_reconciliation, "retry_unresolved_matches", lambda: {
        "status": "completed", "rescanned": 1, "newly_matched": 1, "still_unresolved": 0,
        "financial_effects_applied": False})
    isolated.import_jobs.insert_one({"org_id": "org_default", "import_id": "import_synthetic"})

    result = imports.process_job({"import_id": "import_synthetic", "invoicing_month": "072026"}, "entleistung")

    assert result["status"] == "processed"
    assert result["reconciliation_rematch"]["newly_matched"] == 1


def test_numeric_patient_billing_lookup_and_scoping(isolated):
    isolated.care_events.insert_one({"org_id": "org_default", "care_event_id": "evt", "patient_id": 1000})
    isolated.billing_details.insert_one({"org_id": "org_default", "care_event_id": "evt", "billing_detail_id": "bill"})
    isolated.billing_details.insert_one({"org_id": "other", "care_event_id": "evt", "billing_detail_id": "hidden"})
    rows = billing.get_patient_pending_billing("1000")["billing_details"]
    assert len(rows) == 1 and rows[0]["billing_detail_id"] == "bill"


def test_analytics_accept_both_date_formats_and_sort_years():
    rows = analytics._format_date_grouped_analytics([
        {"_id": "01.12.25", "record_count": 1, "total_amount": 100},
        {"_id": "01.01.2026", "record_count": 1, "total_amount": 200}])
    assert sum(row["total_amount"] for row in rows) == 300
    months = [row["month"] for row in rows]
    assert months.index("12/2025") < months.index("01/2026")


def test_patient_creation_keeps_debtor_number(isolated):
    result = patients.create_patient(patients.PatientRequest(name="Synthetic", birthdate="01.01.1950",
        insurance_number="SYNTHETIC", care_level="1", debtor_number="D-SYNTHETIC"))
    assert isolated.patient_profiles.find_one()["debtor_id"] == "D-SYNTHETIC"
    assert patients.get_patient(result["patient_id"])["patient"]["debtor_number"] == "D-SYNTHETIC"


def test_pdf_uses_billing_coverage_and_escapes_markup(tmp_path, monkeypatch):
    monkeypatch.setattr(invoice_generator, "OUTPUT_DIR", str(tmp_path))
    amounts = database._resolve_invoice_amounts("Entleistung", {"sum_total": 200, "sum_covered": 127.35},
        {"sum_total": 200, "sum_covered": 198, "amount_owed": 2})
    case = {"generation_id": "synthetic", "invoice": {**amounts, "event_type": "Entleistung", "invoice_number": 1},
        "patient": {"name": '<img src="https://review.invalid/synthetic">', "address": ""}, "services": []}
    path = invoice_generator.generate_invoice_pdf(case)
    assert Path(path).name == "RE_1_img_src_https_review_invalid_synthetic.pdf"
    with fitz.open(path) as document:
        text = "".join(page.get_text() for page in document)
    assert "198,00" in text and "2,00" in text
    assert "<img" in text
    with pytest.raises(ValueError):
        invoice_generator.deny_resource_fetch("file:///synthetic")


def test_pdf_endpoint_uses_exact_persisted_path(isolated, tmp_path, monkeypatch):
    monkeypatch.setattr(invoice_generator, "OUTPUT_DIR", str(tmp_path))
    isolated.billing_details.insert_one({"org_id": "org_default", "billing_detail_id": "bill",
        "invoice_number": 12, "pdf_path": "RE_12_exact.pdf"})
    (tmp_path / "RE_12_exact.pdf").write_bytes(b"%PDF-synthetic")
    (tmp_path / "RE_112_other.pdf").write_bytes(b"%PDF-other")
    response = billing.view_billing_pdf("bill")
    assert Path(response.path).name == "RE_12_exact.pdf"
    isolated.billing_details.update_one({}, {"$set": {"pdf_path": "../outside.pdf"}})
    with pytest.raises(HTTPException):
        billing.view_billing_pdf("bill")


def test_health_does_not_claim_ready_on_ping_alone(monkeypatch):
    import backend

    monkeypatch.setattr(backend, "mongodb_health_check", lambda: True)
    monkeypatch.setattr(backend.app.state, "ready", False)
    client = TestClient(backend.app)
    assert client.get("/health").status_code == 503
    for path in ("/patients", "/billing/patients", "/docs"):
        assert client.get(path).status_code == 401
