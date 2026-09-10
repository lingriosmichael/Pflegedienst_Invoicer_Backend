import traceback
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import DEFAULT_ORG_ID
from app.db.mongodb_config import get_database
from app.invoice_issuance import ensure_service_packets
from app.utils.validation import BillingMonth

import app.database as database
import app.invoice_generator as invoice_generator
import app.pdf_parser as pdf_parser
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()

class InvoiceRequest(BaseModel):
    abrechnungsmonat: BillingMonth


def _sgbxi_candidates(invoicing_month: str):
    """Return only normal SGB XI workflow rows, never Entlastungsleistung."""
    database_connection = get_database()
    bills = list(database_connection.billing_details.find({
        "org_id": DEFAULT_ORG_ID,
        "invoicing_month": invoicing_month,
        "billing_status": "invoice_needed",
    }).sort("billing_detail_id", 1))
    events = {
        event["care_event_id"]: event
        for event in database_connection.care_events.find({
            "org_id": DEFAULT_ORG_ID,
            "care_event_id": {"$in": [bill["care_event_id"] for bill in bills]},
            "event_type": {"$in": ["SGBXI", "ServicePacket"]},
        })
    }
    patients = {
        patient["patient_id"]: patient
        for patient in database_connection.patient_profiles.find({
            "org_id": DEFAULT_ORG_ID,
            "patient_id": {"$in": [event["patient_id"] for event in events.values()]},
        })
    }
    candidates = []
    for bill in bills:
        event = events.get(bill["care_event_id"])
        if not event:
            continue
        patient = patients.get(event["patient_id"], {})
        candidates.append({
            "billing_detail_id": bill["billing_detail_id"],
            "patient_name": patient.get("patient_name", "Unbekannter Patient"),
            "care_event_id": event["care_event_id"],
            "event_type": event["event_type"],
            "service_period": f"{event.get('period_start_date', '')} – {event.get('period_end_date', '')}",
            "sum_total": bill.get("sum_total", 0),
            "sum_covered": bill.get("sum_covered", 0),
            "investment_cost": bill.get("investitionskosten", 0),
            "service_packet_amount": bill.get("service_packet_amount", 0),
            "amount_owed": bill.get("amount_owed", 0),
            "invoice_total": round((bill.get("amount_owed", 0) or 0) + (bill.get("service_packet_amount", 0) or 0), 2),
        })
    return candidates


@router.post("/sgbxi-invoices/prepare")
def prepare_sgbxi_invoices(req: InvoiceRequest):
    """Prepare and list normal SGB XI invoices for explicit staff selection."""
    created = database.mark_month_ready_for_generation(
        req.abrechnungsmonat,
        event_types=["SGBXI", "ServicePacket"],
    )
    # This can attach the monthly €40 packet to the appropriate SGB XI bill,
    # or create a packet-only SGB XI workflow row. It never sees Entleistung.
    ensure_service_packets(req.abrechnungsmonat)
    return {
        "status": "ok",
        "created": created,
        "invoicing_month": req.abrechnungsmonat,
        "candidates": _sgbxi_candidates(req.abrechnungsmonat),
    }


@router.post("/sgbxi-invoices/issue-all")
def issue_all_sgbxi_invoices(req: InvoiceRequest):
    """Render every currently open SGB XI invoice for the requested month."""
    billing_detail_ids = [candidate["billing_detail_id"] for candidate in _sgbxi_candidates(req.abrechnungsmonat)]
    result = invoice_generator.process_generate_invoices(
        req.abrechnungsmonat,
        include_orphaned=False,
        require_invoice_needed=True,
        billing_detail_ids=billing_detail_ids,
        allowed_event_types={"SGBXI", "ServicePacket"},
    )
    return {
        "status": "ok",
        "message": "SGB-XI-Rechnungen erstellt",
        "invoicing_month": req.abrechnungsmonat,
        **result,
    }

@router.post("/generate_invoices")
def generate_invoices(req: InvoiceRequest):
    raise HTTPException(410, "Use /sgbxi-invoices/prepare and issue-selected to choose SGB XI invoices explicitly")


@router.post("/regenerate_invoices")
def regenerate_invoices(req: InvoiceRequest):
    """
    Regenerate PDFs for an already prepared month without re-running mark_ready.

    This reuses the existing invoice numbers where present and only includes
    billing rows whose linked care_event period actually belongs to the
    requested month.
    """
    result = invoice_generator.process_generate_invoices(
        req.abrechnungsmonat,
        include_orphaned=False,
        require_invoice_needed=False,
        allowed_event_types={"SGBXI", "ServicePacket"},
    )
    return {
        "status": "ok",
        "message": "Rechnungen neu erstellt",
        "abrechnungsmonat": req.abrechnungsmonat,
        "generated": result["generated"],
        "total_cases": result["total_cases"],
        "failed": result["failed"],
    }


@router.post("/complete_data")
def complete_data(req: InvoiceRequest):
    database.check_missing_patient_fields(req.abrechnungsmonat, auto_fix=True)
    database.check_service_fields(auto_fix=True)
    return {"status": "ok", "message": "Patientendaten ergänzt und Leistungsdaten überprüft"}


@router.post("/check_service_fields")
def check_service_fields():
    database.check_service_fields(auto_fix=False)
    return {"status": "ok", "message": "Leistungsdaten überprüft"}


class MarkReadyRequest(BaseModel):
    invoicing_month: BillingMonth   # "MMYYYY"
    only_positive: bool = False

@router.post("/mark_ready")
def mark_ready(req: MarkReadyRequest):
    try:
        m = (req.invoicing_month or "").strip()
        if len(m) != 6 or not m.isdigit():
            raise HTTPException(status_code=400, detail="invoicing_month must be MMYYYY")

        updated = database.mark_month_ready_for_generation(
            m, req.only_positive, event_types=["SGBXI", "ServicePacket"]
        )
        return {"status": "ok", "updated": updated}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Operation failed; verify input and database readiness")

@router.post("/validate_sgbxi_amounts")
def validate_sgbxi_amounts():
    raise HTTPException(status_code=409, detail="Review invalid import records by import ID; global historical repair is disabled")
