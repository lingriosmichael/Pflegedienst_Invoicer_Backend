from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.routers.imports import file_hash, resolve_pdf
from app import pdf_parser
from app.db import DEFAULT_ORG_ID
from app.db.mongodb_config import get_database
from app.entlastung_replay import reference_year_trajectory
from app.database import expire_stale_entlastung_coverage
from app.utils.data_validation import DataNormalizer
from app.rzh_reconciliation import (billing_detail_already_issued, confirm_private_amount, dismiss_item,
                                    eligible_invoice_queue, issue_confirmed_invoice, item_detail, manually_match_item,
                                    parse_settlement_text, persist_settlement_observations, register_batch,
                                    retry_unresolved_matches, invoice_selection_queue, approve_invoice_selection,
                                    parse_settlement_pdf)


router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])
PARSER_VERSION = "rzh_reconciliation_evidence_v1"


class RegisterStatementRequest(BaseModel):
    file_name: str


class ManualMatchRequest(BaseModel):
    care_event_id: str


class DismissItemRequest(BaseModel):
    reason: str


class InvoiceSelectionRequest(BaseModel):
    billing_detail_ids: list[str]


@router.post("/register-source")
def register_source_statement(request: RegisterStatementRequest):
    """Register an uploaded RZH statement without parsing or applying it."""
    path = resolve_pdf(request.file_name)
    batch = register_batch(path.name, file_hash(path), PARSER_VERSION)
    return {
        "status": "already_registered" if batch["already_registered"] else "registered",
        "batch_id": batch["batch_id"],
        "parse_status": batch["parse_status"],
        "financial_effects_applied": False,
    }


@router.post("/parse")
def parse_statement(request: RegisterStatementRequest):
    """Parse and match settlement evidence without applying financial effects."""
    path = resolve_pdf(request.file_name)
    batch = register_batch(path.name, file_hash(path), PARSER_VERSION)
    observations, warnings = parse_settlement_pdf(path)
    result = persist_settlement_observations(batch, observations, warnings, PARSER_VERSION)
    return {"status": "parsed" if not warnings else "incomplete", "batch_id": batch["batch_id"], **result}


@router.get("/items")
def list_items(match_status: str | None = None, apply_status: str | None = None):
    expire_stale_entlastung_coverage()
    query = {"org_id": DEFAULT_ORG_ID, "section_type": {"$ne": "gutschrift"}}
    if match_status:
        query["match_status"] = match_status
    if apply_status:
        query["apply_status"] = apply_status
    database = get_database()
    items = list(database.rzh_reconciliation_items.find(query).sort("created_at", -1))

    # Batch-load each involved patient's pure statutory Sec.45b reference
    # trajectory (see reference_year_trajectory) so the table can show, per
    # row, what the correct monthly-accrual/carry-forward calculation says was
    # available right before that specific reduction -- covering Jan-Dec 2026
    # uniformly, not just the operational July-forward ledger. This is a
    # read-only recomputation for display; it never touches billing_details,
    # invoices, or entlastung_year_balance.
    patient_ids = {item["patient_id"] for item in items if item.get("patient_id")}
    trajectories = {patient_id: reference_year_trajectory(patient_id, database) for patient_id in patient_ids}
    profiles = {
        profile["patient_id"]: profile
        for profile in database.patient_profiles.find({"org_id": DEFAULT_ORG_ID, "patient_id": {"$in": list(patient_ids)}})
    } if patient_ids else {}

    # Batch-load billing_details for every matched care event so the review
    # list can flag up front -- before staff click "Bestätigen" -- that a
    # claim already has an invoice out and can't go through the normal
    # confirm flow (see billing_detail_already_issued / confirm_private_amount).
    care_event_ids = [item["matched_care_event_id"] for item in items if item.get("matched_care_event_id")]
    bills_by_event = {
        bill["care_event_id"]: bill
        for bill in database.billing_details.find({"org_id": DEFAULT_ORG_ID, "care_event_id": {"$in": care_event_ids}})
    } if care_event_ids else {}

    rows = []
    for item in items:
        row = {key: item.get(key) for key in ("reconciliation_item_id", "section_type", "reason_code",
            "amount_cents", "match_status", "match_method", "patient_id", "matched_care_event_id",
            "candidate_care_event_ids", "apply_status", "service_period_start", "service_period_end",
            "original_invoice_number", "created_at", "last_observed_batch_id",
            "patient_name_raw", "insurance_number", "care_account", "reason_raw",
            "dismissed_reason", "dismissed_at", "dismissed_by")}
        # Resolved patient name (falls back to the raw PDF name if not yet matched).
        profile = profiles.get(item.get("patient_id"))
        row["patient_name"] = DataNormalizer.normalize_patient_name(profile["patient_name"]) if profile else None
        entry = trajectories.get(item.get("patient_id"), {}).get(item.get("matched_care_event_id"))
        row["entlastung_available_before"] = entry.get("available_before") if entry else None
        row["entlastung_reference_covered"] = entry.get("reference_covered") if entry else None
        row["entlastung_reference_owed"] = entry.get("reference_owed") if entry else None
        row["entlastung_balance_after"] = entry.get("balance_after") if entry else None
        row["already_invoiced"] = billing_detail_already_issued(bills_by_event.get(item.get("matched_care_event_id")))
        rows.append(row)
    return {"status": "ok", "items": rows}


@router.get("/items/{reconciliation_item_id}/detail")
def get_item_detail(reconciliation_item_id: str):
    """The exact RZH-statement evidence for one reduction plus the claim it matched to, for the expanded row view."""
    return item_detail(reconciliation_item_id)


@router.get("/batches")
def list_batches():
    rows = []
    for batch in get_database().rzh_reconciliation_batches.find({"org_id": DEFAULT_ORG_ID}).sort("created_at", -1):
        rows.append({key: batch.get(key) for key in ("batch_id", "source_file_name", "parse_status", "item_count",
            "matched_count", "manual_review_count", "warnings", "parser_versions", "created_at", "last_parsed_at")})
    return {"status": "ok", "batches": rows}


@router.post("/retry-matching")
def retry_matching():
    return retry_unresolved_matches()


@router.post("/items/{reconciliation_item_id}/match")
def match_item_manually(reconciliation_item_id: str, request: ManualMatchRequest):
    return manually_match_item(reconciliation_item_id, request.care_event_id)


@router.post("/items/{reconciliation_item_id}/confirm-private-amount")
def confirm_item_private_amount(reconciliation_item_id: str, request: Request):
    return confirm_private_amount(reconciliation_item_id, request.state.actor)


@router.post("/items/{reconciliation_item_id}/dismiss")
def dismiss_reconciliation_item(reconciliation_item_id: str, request: DismissItemRequest, request_context: Request):
    """Close out a reconciliation item without a private-amount confirmation (see dismiss_item)."""
    return dismiss_item(reconciliation_item_id, request_context.state.actor, request.reason)


@router.get("/eligible-invoices")
def list_eligible_invoices():
    return {"status": "ok", "invoices": eligible_invoice_queue()}


@router.get("/invoice-candidates")
def list_invoice_candidates():
    return {"status": "ok", "candidates": invoice_selection_queue()}


@router.post("/invoice-candidates/approve")
def approve_invoice_candidates(request: InvoiceSelectionRequest, request_context: Request):
    return approve_invoice_selection(request.billing_detail_ids, request_context.state.actor)


@router.post("/eligible-invoices/{billing_detail_id}/issue")
def issue_eligible_invoice(billing_detail_id: str):
    return issue_confirmed_invoice(billing_detail_id)
