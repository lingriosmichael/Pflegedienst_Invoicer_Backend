import mongomock
import pytest

from fastapi import HTTPException

from app.db import mongodb_config
from app.rzh_reconciliation import (
    ENTITLEMENT_EXHAUSTED,
    ENTITLEMENT_MONTHLY_MAX_EXCEEDED,
    LATE_PAYMENT_CREDIT,
    NONPAYMENT_COLLECTION,
    OTHER_MANUAL_REVIEW,
    billing_detail_already_issued,
    classify_reason,
    confirm_private_amount,
    dismiss_item,
    eligible_invoice_queue,
    issue_confirmed_invoice,
    item_detail,
    manually_match_item,
    match_item,
    parse_german_cents,
    parse_settlement_pdf,
    parse_settlement_text,
    persist_settlement_observations,
    register_batch,
    retry_unresolved_matches,
    settlement_item_fingerprint,
)


def _build_settlement_pdf(tmp_path, pages_lines):
    """Render synthetic (non-patient-derived) rows into an RZH-shaped PDF fixture.

    Reproduces the real statement's word layout closely enough to exercise the
    position-aware parser: one page per list of lines, each line at its own Y,
    section headings and page breaks placed exactly as staff supply them so a
    section can span a page break without repeating its heading or header row.
    """
    import fitz

    document = fitz.open()
    for lines in pages_lines:
        page = document.new_page(width=800, height=60 + 20 * (len(lines) + 2))
        y = 40
        for line in lines:
            page.insert_text((40, y), line, fontsize=10)
            y += 20
    path = tmp_path / "synthetic_rzh_statement.pdf"
    document.save(str(path))
    document.close()
    return path


def test_reason_classification_uses_explicit_settlement_rules():
    assert classify_reason("Der Höchstbetrag wurde ausgeschöpft") == ENTITLEMENT_EXHAUSTED
    assert classify_reason("Der monatliche Höchstbetrag wurde überschritten") == ENTITLEMENT_MONTHLY_MAX_EXCEEDED
    assert classify_reason("Absetzung, da trotz Mahnung keine Zahlung erfolgte") == NONPAYMENT_COLLECTION
    assert classify_reason("Gutschrift wegen nachträglichem Zahlungseingang") == LATE_PAYMENT_CREDIT
    assert classify_reason("Unbekannte Abweichung") == OTHER_MANUAL_REVIEW


def test_settlement_identity_ignores_parser_and_batch_metadata():
    item = {
        "rzh_transaction_number": "TX-1", "original_invoice_number": "INV-1",
        "insurance_number": "SYNTHETIC", "care_account": "4064",
        "service_period_start": "2026-07-01", "service_period_end": "2026-07-31",
        "amount_cents": -8125, "section_type": "absetzung",
        "batch_id": "first", "parser_version": "v1",
    }
    reparsed = {**item, "batch_id": "second", "parser_version": "v2"}
    assert settlement_item_fingerprint(item) == settlement_item_fingerprint(reparsed)


def test_parser_requires_complete_patient_level_settlement_evidence():
    text = """Absetzungen
Vorgangsnummer: TX-1
Originalrechnungsnummer: INV-1
Verordnung: Synthetic Patient
Versichertennummer: SYNTHETIC
Pflegekonto: 4064
Pflegezeitraum: 01.07.2026 - 31.07.2026
Absetzungsbetrag: -81,25
Begründung: Der Höchstbetrag wurde ausgeschöpft
"""
    items, warnings = parse_settlement_text(text)

    assert not warnings
    assert items == [{
        "section_type": "absetzung", "rzh_transaction_number": "TX-1", "original_invoice_number": "INV-1",
        "patient_name_raw": "Synthetic Patient", "insurance_number": "SYNTHETIC", "care_account": "4064",
        "service_period_start": "01.07.2026", "service_period_end": "31.07.2026", "amount_cents": -8125,
        "reason_raw": "Der Höchstbetrag wurde ausgeschöpft", "reason_code": ENTITLEMENT_EXHAUSTED,
    }]

    incomplete, incomplete_warnings = parse_settlement_text(text.replace("Absetzungsbetrag: -81,25\n", ""))
    assert incomplete == []
    assert incomplete_warnings


def test_parser_stops_before_normal_service_sections():
    text = """Absetzungen
Vorgangsnummer: TX-1
Originalrechnungsnummer: INV-1
Verordnung: Synthetic Patient
Versichertennummer: SYNTHETIC
Pflegekonto: 4064
Pflegezeitraum: 01.07.2026 - 31.07.2026
Absetzungsbetrag: -81,25
Begründung: Der Höchstbetrag wurde ausgeschöpft
Abgerechnete Belege
Verordnung: Must Not Be Parsed
Versichertennummer: SYNTHETIC
Pflegekonto: 4064
Pflegezeitraum: 01.08.2026 - 31.08.2026
Absetzungsbetrag: -20,00
Begründung: Der Höchstbetrag wurde ausgeschöpft
"""
    items, warnings = parse_settlement_text(text)
    assert len(items) == 1
    assert not warnings


def test_money_parser_and_matcher_do_not_guess_ambiguous_events():
    assert parse_german_cents("-1.843,82") == -184382
    database = mongomock.MongoClient().test
    database.patient_profiles.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "insurance_number": "SYNTHETIC"})
    for identifier in ("evt_one", "evt_two"):
        database.care_events.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "care_event_id": identifier,
            "event_type": "Entleistung", "care_account": "4064", "period_start_date": "01.07.2026", "period_end_date": "31.07.2026"})
    item = {"insurance_number": "SYNTHETIC", "service_period_start": "01.07.2026", "service_period_end": "31.07.2026"}
    assert match_item(item, database)["match_status"] == "ambiguous"


def test_persisted_observation_is_idempotent_without_financial_effects(monkeypatch):
    database = mongomock.MongoClient().test
    monkeypatch.setattr(mongodb_config, "_database", database)
    batch = register_batch("synthetic.pdf", "source-hash", "parser-v1")
    observation = {
        "section_type": "absetzung", "rzh_transaction_number": "TX-1", "original_invoice_number": "INV-1",
        "insurance_number": "MISSING", "care_account": "4064", "service_period_start": "01.07.2026",
        "service_period_end": "31.07.2026", "amount_cents": -8125,
        "reason_raw": "Der Höchstbetrag wurde ausgeschöpft", "reason_code": ENTITLEMENT_EXHAUSTED,
    }
    first = persist_settlement_observations(batch, [observation], [], "parser-v1")
    repeated = persist_settlement_observations(batch, [observation], [], "parser-v2")

    assert first["financial_effects_applied"] is False
    assert repeated["financial_effects_applied"] is False
    assert database.rzh_reconciliation_items.count_documents({}) == 1
    assert database.billing_details.count_documents({}) == 0
    assert database.entlastung_year_balance.count_documents({}) == 0


def test_manual_match_and_confirmed_invoice_queue_are_review_only(monkeypatch):
    database = mongomock.MongoClient().test
    monkeypatch.setattr(mongodb_config, "_database", database)
    database.patient_profiles.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "insurance_number": "SYNTHETIC"})
    database.care_events.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "care_event_id": "evt_synthetic",
        "event_type": "Entleistung", "care_account": "4064", "period_start_date": "01.07.2026", "period_end_date": "31.07.2026"})
    database.rzh_reconciliation_items.insert_one({"org_id": "org_default", "reconciliation_item_id": "rri_synthetic",
        "item_fingerprint": "synthetic", "insurance_number": "SYNTHETIC", "reason_code": ENTITLEMENT_EXHAUSTED,
        "match_status": "unmatched", "apply_status": "not_applied"})

    matched = manually_match_item("rri_synthetic", "evt_synthetic")
    assert matched["financial_effects_applied"] is False

    database.billing_details.insert_one({"org_id": "org_default", "billing_detail_id": "bill_synthetic",
        "care_event_id": "evt_synthetic", "invoicing_month": "072026", "amount_owed": 81.25,
        "billing_status": "invoice_needed", "reconciliation_status": "confirmed_limit_partial", "invoice_number": None})
    queue = eligible_invoice_queue(database)
    assert [row["billing_detail_id"] for row in queue] == ["bill_synthetic"]


def test_manual_confirmation_promotes_only_matched_maximum_limit_item(monkeypatch):
    database = mongomock.MongoClient().test
    monkeypatch.setattr(mongodb_config, "_database", database)
    database.patient_profiles.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "insurance_number": "SYNTHETIC"})
    database.care_events.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "care_event_id": "evt_synthetic",
        "event_type": "Entleistung", "care_account": "4064", "invoicing_month": "072026", "sum_total": 212.25,
        "period_start_date": "01.07.2026", "period_end_date": "31.07.2026"})
    database.billing_details.insert_one({"org_id": "org_default", "billing_detail_id": "bill_synthetic",
        "care_event_id": "evt_synthetic", "invoicing_month": "072026", "sum_total": 212.25, "sum_covered": 212.25,
        "amount_owed": 0.0, "billing_status": "covered_insurance", "reconciliation_status": "pending_no_exception"})
    database.rzh_reconciliation_items.insert_one({"org_id": "org_default", "reconciliation_item_id": "rri_synthetic",
        "item_fingerprint": "synthetic", "reason_code": ENTITLEMENT_EXHAUSTED, "amount_cents": -8125,
        "match_status": "matched", "matched_care_event_id": "evt_synthetic", "apply_status": "not_applied"})

    result = confirm_private_amount("rri_synthetic", "synthetic-admin")

    bill = database.billing_details.find_one({"billing_detail_id": "bill_synthetic"})
    assert result["confirmed_amount_owed"] == 81.25
    assert result["ledger_effects_applied"] is False
    assert bill["billing_status"] == "invoice_needed"
    assert bill["reconciliation_status"] == "confirmed_limit_partial"
    assert bill["sum_covered"] == 131.0
    assert bill["amount_owed"] == 81.25
    assert database.entlastung_year_balance.count_documents({}) == 0

    repeated = confirm_private_amount("rri_synthetic", "synthetic-admin")
    assert repeated["already_confirmed"] is True
    assert database.care_event_history.count_documents({"action": "rzh_private_amount_confirmed"}) == 1


def test_confirmation_creates_a_missing_historic_billing_marker(monkeypatch):
    database = mongomock.MongoClient().test_database
    monkeypatch.setattr(mongodb_config, "_database", database)
    database.care_events.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "care_event_id": "evt_historic",
        "event_type": "Entleistung", "care_account": "4064", "invoicing_month": "042026", "sum_total": 84.90})
    database.rzh_reconciliation_items.insert_one({"org_id": "org_default", "reconciliation_item_id": "rri_historic",
        "item_fingerprint": "historic", "reason_code": NONPAYMENT_COLLECTION, "amount_cents": -8490,
        "match_status": "matched", "matched_care_event_id": "evt_historic", "apply_status": "not_applied"})

    result = confirm_private_amount("rri_historic", "synthetic-admin")

    bill = database.billing_details.find_one({"care_event_id": "evt_historic"})
    assert result["billing_detail_id"] == bill["billing_detail_id"]
    assert bill["amount_owed"] == 84.90
    assert bill["billing_status"] == "invoice_needed"


def test_billing_detail_already_issued_matches_the_confirm_private_amount_guard():
    assert billing_detail_already_issued(None) is False
    assert billing_detail_already_issued({"billing_status": "invoice_needed"}) is False
    assert billing_detail_already_issued({"billing_status": "sent"}) is True
    assert billing_detail_already_issued({"billing_status": "paid"}) is True
    assert billing_detail_already_issued({"billing_status": "invoice_needed", "invoice_number": "R-1"}) is True
    assert billing_detail_already_issued({"billing_status": "invoice_needed", "pdf_path": "/x.pdf"}) is True


def test_dismiss_item_closes_a_case_without_touching_billing_or_ledger(monkeypatch):
    """Staff need a way to close out a reconciliation item that will never
    clear confirm_private_amount (e.g. an already-issued invoice needing a
    manual accounting correction) without hiding it -- it must stay visible,
    keep its evidence, and record who closed it and why, while stopping it
    from counting as an open review case.
    """
    database = mongomock.MongoClient().test
    monkeypatch.setattr(mongodb_config, "_database", database)
    database.rzh_reconciliation_items.insert_one({"org_id": "org_default", "reconciliation_item_id": "rri_dismiss",
        "item_fingerprint": "synthetic-dismiss", "reason_code": NONPAYMENT_COLLECTION, "amount_cents": -12370,
        "match_status": "matched", "matched_care_event_id": "evt_already_invoiced", "apply_status": "not_applied"})

    with pytest.raises(HTTPException) as missing_reason:
        dismiss_item("rri_dismiss", "synthetic-admin", "  ")
    assert missing_reason.value.status_code == 422

    result = dismiss_item("rri_dismiss", "synthetic-admin", "Bereits fakturiert, manuelle Korrektur nötig")

    assert result["status"] == "dismissed"
    assert result["financial_effects_applied"] is False
    item = database.rzh_reconciliation_items.find_one({"reconciliation_item_id": "rri_dismiss"})
    assert item["apply_status"] == "dismissed"
    assert item["dismissed_reason"] == "Bereits fakturiert, manuelle Korrektur nötig"
    assert item["dismissed_by"] == "synthetic-admin"
    assert database.billing_details.count_documents({}) == 0
    assert database.entlastung_year_balance.count_documents({}) == 0
    history = database.care_event_history.find_one({"action": "rzh_reconciliation_item_dismissed"})
    assert history["care_event_id"] == "evt_already_invoiced"
    assert history["reason"] == "Bereits fakturiert, manuelle Korrektur nötig"

    with pytest.raises(HTTPException) as already_dismissed:
        dismiss_item("rri_dismiss", "synthetic-admin", "erneuter Versuch")
    assert already_dismissed.value.status_code == 409


def test_issue_confirmed_invoice_only_delegates_an_eligible_queue_item(monkeypatch):
    database = mongomock.MongoClient().test
    monkeypatch.setattr(mongodb_config, "_database", database)
    database.care_events.insert_one({"org_id": "org_default", "care_event_id": "evt_synthetic",
        "event_type": "Entleistung", "invoicing_month": "072026"})
    database.billing_details.insert_one({"org_id": "org_default", "billing_detail_id": "bill_synthetic",
        "care_event_id": "evt_synthetic", "invoicing_month": "072026", "amount_owed": 81.25,
        "billing_status": "invoice_needed", "reconciliation_status": "confirmed_limit_partial"})

    generated = []
    def fake_generate(billing_detail_id):
        generated.append(billing_detail_id)
        database.billing_details.update_one({"billing_detail_id": billing_detail_id},
            {"$set": {"invoice_number": 12, "pdf_path": "RE_12_synthetic.pdf"}})
    import app.invoice_generator
    monkeypatch.setattr(app.invoice_generator, "generate_billing_pdf", fake_generate)

    result = issue_confirmed_invoice("bill_synthetic")

    assert result == {"status": "generated", "billing_detail_id": "bill_synthetic", "invoice_number": 12}
    assert generated == ["bill_synthetic"]
    assert issue_confirmed_invoice("bill_synthetic")["status"] == "already_generated"

    database.billing_details.update_one({"billing_detail_id": "bill_synthetic"},
        {"$set": {"pdf_path": None, "reconciliation_status": "pending_shortfall"}})
    with pytest.raises(HTTPException, match="not an RZH-confirmed"):
        issue_confirmed_invoice("bill_synthetic")


def test_retry_unresolved_matches_links_later_import_without_financial_effect(monkeypatch):
    database = mongomock.MongoClient().test
    monkeypatch.setattr(mongodb_config, "_database", database)
    database.patient_profiles.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "insurance_number": "SYNTHETIC"})
    database.rzh_reconciliation_items.insert_one({"org_id": "org_default", "reconciliation_item_id": "rri_synthetic",
        "insurance_number": "SYNTHETIC", "service_period_start": "01.07.2026", "service_period_end": "31.07.2026",
        "match_status": "unmatched", "apply_status": "not_applied"})
    database.care_events.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "care_event_id": "evt_synthetic",
        "event_type": "Entleistung", "care_account": "4064", "period_start_date": "01.07.2026", "period_end_date": "31.07.2026"})

    result = retry_unresolved_matches()

    item = database.rzh_reconciliation_items.find_one({"reconciliation_item_id": "rri_synthetic"})
    assert result == {"status": "completed", "rescanned": 1, "newly_matched": 1, "still_unresolved": 0,
                      "financial_effects_applied": False}
    assert item["match_status"] == "matched"
    assert item["matched_care_event_id"] == "evt_synthetic"
    assert database.billing_details.count_documents({}) == 0
    assert database.entlastung_year_balance.count_documents({}) == 0


def test_pdf_parser_splits_a_grouped_header_into_exact_patient_level_lines(tmp_path):
    path = _build_settlement_pdf(tmp_path, [[
        "Absetzungen",
        "01.07.2026 DMK900000002 Test Kasse 200000-2026000002 -150,00",
        "Begründung Der monatliche Höchstbetrag wurde überschritten",
        "Verordnung ONE, PATIENT - A111111111 - Status: 10001 -50,00",
        "Verordnungsdatum: 01.06.26, Pflegezeitraum: 01.06.26 - 15.06.26, Pflegekonto: 4064",
        "Verordnung TWO, PATIENT - B222222222 - Status: 10001 -100,00",
        "Verordnungsdatum: 01.06.26, Pflegezeitraum: 16.06.26 - 30.06.26, Pflegekonto: 4064",
    ]])

    items, warnings = parse_settlement_pdf(path)

    assert not warnings
    assert [item["insurance_number"] for item in items] == ["A111111111", "B222222222"]
    assert [item["amount_cents"] for item in items] == [-5000, -10000]
    assert {item["reason_code"] for item in items} == {ENTITLEMENT_MONTHLY_MAX_EXCEEDED}
    assert {item["rzh_transaction_number"] for item in items} == {"DMK900000002"}
    assert {item["original_invoice_number"] for item in items} == {"200000-2026000002"}


def test_pdf_parser_never_guesses_an_unbalanced_grouped_header(tmp_path):
    path = _build_settlement_pdf(tmp_path, [[
        "Absetzungen",
        "01.07.2026 DMK900000003 Test Kasse 200000-2026000003 -140,00",
        "Begründung Der monatliche Höchstbetrag wurde überschritten",
        "Verordnung ONE, PATIENT - A111111111 - Status: 10001 -50,00",
        "Verordnungsdatum: 01.06.26, Pflegezeitraum: 01.06.26 - 15.06.26, Pflegekonto: 4064",
        "Verordnung TWO, PATIENT - B222222222 - Status: 10001 -100,00",
        "Verordnungsdatum: 01.06.26, Pflegezeitraum: 16.06.26 - 30.06.26, Pflegekonto: 4064",
    ]])

    items, warnings = parse_settlement_pdf(path)

    assert items == []
    assert warnings == ["Grouped RZH transaction amount does not reconcile to patient-level lines"]


def test_pdf_parser_ignores_settlement_lines_outside_care_account_4064(tmp_path):
    path = _build_settlement_pdf(tmp_path, [[
        "Absetzungen",
        "01.07.2026 DMK900000004 Test Kasse 200000-2026000004 -50,00",
        "Begründung Die Leistung wurde doppelt in Rechnung gestellt.",
        "Verordnung THREE, PATIENT - C333333333 - Status: 10001 -50,00",
        "Verordnungsdatum: 01.06.26, Pflegezeitraum: 01.06.26 - 30.06.26, Pflegekonto: 4062",
    ]])

    items, warnings = parse_settlement_pdf(path)

    assert items == []
    assert not warnings


def test_pdf_parser_continues_a_section_across_a_page_break_without_repeated_heading(tmp_path):
    path = _build_settlement_pdf(tmp_path, [
        [
            "Absetzungen",
            "01.07.2026 DMK900000005 Test Kasse 300000-2026000005 -50,00",
        ],
        [
            "Begründung Der Höchstbetrag wurde ausgeschöpft",
            "Verordnung FOUR, PATIENT - D444444444 - Status: 10001 -50,00",
            "Verordnungsdatum: 01.06.26, Pflegezeitraum: 01.06.26 - 30.06.26, Pflegekonto: 4064",
        ],
    ])

    items, warnings = parse_settlement_pdf(path)

    assert not warnings
    assert len(items) == 1
    assert items[0]["insurance_number"] == "D444444444"
    assert items[0]["amount_cents"] == -5000
    assert items[0]["reason_code"] == ENTITLEMENT_EXHAUSTED


def test_pdf_parser_handles_wrapped_pflegekonto_and_non_dash_invoice_reference(tmp_path):
    path = _build_settlement_pdf(tmp_path, [[
        "Absetzungen",
        "01.07.2026 DMK900000006 Test Kasse SAPV DR900000006 -50,00",
        "Begründung Absetzung, da trotz Mahnung keine Zahlung erfolgte.",
        "Verordnung FIVE, PATIENT - E555555555 - Status: 10001 -50,00",
        "Verordnungsdatum: 01.06.26, Pflegezeitraum: 01.06.26 - 30.06.26, Pflegekonto:",
        "4064",
    ]])

    items, warnings = parse_settlement_pdf(path)

    assert not warnings
    assert len(items) == 1
    assert items[0]["original_invoice_number"] == "DR900000006"
    assert items[0]["care_account"] == "4064"
    assert items[0]["reason_code"] == NONPAYMENT_COLLECTION


def test_pdf_parser_does_not_mistake_a_carrier_name_for_an_invoice_reference(tmp_path):
    """If the 'Urspr. Rechnung' column is ever blank, the last word of the
    carrier name must not be silently captured as original_invoice_number.
    """
    path = _build_settlement_pdf(tmp_path, [[
        "Absetzungen",
        "01.07.2026 DMK900000007 Test Kasse Sachsen -50,00",
        "Begründung Der Höchstbetrag wurde ausgeschöpft",
        "Verordnung SIX, PATIENT - F666666666 - Status: 10001 -50,00",
        "Verordnungsdatum: 01.06.26, Pflegezeitraum: 01.06.26 - 30.06.26, Pflegekonto: 4064",
    ]])

    items, warnings = parse_settlement_pdf(path)

    assert not warnings
    assert len(items) == 1
    assert items[0]["original_invoice_number"] is None


def test_item_detail_shows_the_matched_claim_alongside_the_raw_correction(monkeypatch):
    database = mongomock.MongoClient().test
    monkeypatch.setattr(mongodb_config, "_database", database)
    database.patient_profiles.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic",
        "patient_name": "Synthetic Patient", "insurance_number": "SYNTHETIC"})
    database.care_events.insert_one({"org_id": "org_default", "patient_id": "pat_synthetic", "care_event_id": "evt_synthetic",
        "event_type": "Entleistung", "care_account": "4064", "invoicing_month": "072026", "sum_total": 212.25,
        "period_start_date": "01.07.2026", "period_end_date": "31.07.2026", "services": [{"description": "Entlastungsleistung"}]})
    database.billing_details.insert_one({"org_id": "org_default", "billing_detail_id": "bill_synthetic",
        "care_event_id": "evt_synthetic", "sum_covered": 131.0, "amount_owed": 81.25,
        "billing_status": "invoice_needed", "reconciliation_status": "confirmed_limit_partial"})
    database.rzh_reconciliation_batches.insert_one({"org_id": "org_default", "batch_id": "rrb_synthetic",
        "source_file_name": "August_2026.pdf"})
    database.rzh_reconciliation_items.insert_one({"org_id": "org_default", "reconciliation_item_id": "rri_synthetic",
        "item_fingerprint": "x", "reason_code": ENTITLEMENT_EXHAUSTED, "reason_raw": "Der Höchstbetrag wurde ausgeschöpft",
        "amount_cents": -8125, "rzh_transaction_number": "DMK900000001", "original_invoice_number": "301601-2026083078",
        "patient_name_raw": "SYNTHETIC, PATIENT", "insurance_number": "SYNTHETIC", "care_account": "4064",
        "service_period_start": "01.07.2026", "service_period_end": "31.07.2026", "batch_id": "rrb_synthetic",
        "match_status": "matched", "matched_care_event_id": "evt_synthetic", "apply_status": "not_applied"})

    detail = item_detail("rri_synthetic")

    assert detail["correction"]["reason_raw"] == "Der Höchstbetrag wurde ausgeschöpft"
    assert detail["correction"]["source_file_name"] == "August_2026.pdf"
    assert detail["claim"]["patient_name"] == "Synthetic Patient"
    assert detail["claim"]["invoicing_month"] == "072026"
    assert detail["claim"]["sum_total"] == 212.25
    assert detail["claim"]["sum_covered"] == 131.0
    assert detail["claim"]["amount_owed"] == 81.25
