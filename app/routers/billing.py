from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.db import DEFAULT_ORG_ID
from app.db.mongodb_config import get_database
from app.utils.validation import NonnegativeAmount, patient_id_filter, parse_service_date
from app.billing_operations import update_services, transition_status


router = APIRouter()


class ServiceUpdate(BaseModel):
    service_code: str
    service_description: str
    quantity_value: NonnegativeAmount
    unit_price: NonnegativeAmount
    line_total: NonnegativeAmount | None = None


class ServicesUpdateRequest(BaseModel):
    services: List[ServiceUpdate]
    sum_covered: NonnegativeAmount | None = None


class BillingStatusUpdate(BaseModel):
    billing_status: str


def find_bill(billing_detail_id):
    bill = get_database().billing_details.find_one({"org_id": DEFAULT_ORG_ID, "billing_detail_id": billing_detail_id})
    if not bill:
        raise HTTPException(404, "Billing detail not found")
    return bill


@router.get("/billing/patients")
def get_billing_patients():
    database = get_database()
    rows = []
    for patient in database.patient_profiles.find({"org_id": DEFAULT_ORG_ID}):
        events = database.care_events.distinct("care_event_id", {"org_id": DEFAULT_ORG_ID, "patient_id": patient["patient_id"]})
        query = {"org_id": DEFAULT_ORG_ID, "care_event_id": {"$in": events}}
        if not database.billing_details.find_one(query):
            continue
        count = database.billing_details.count_documents({**query, "billing_status": {"$in": ["", "invoice_needed", "sent"]}})
        rows.append({"id": patient["patient_id"], "name": patient["patient_name"], "pending_count": count})
    return {"status": "ok", "patients": sorted(rows, key=lambda patient: patient["name"])}


@router.get("/billing/patient/{patient_id}/pending")
def get_patient_pending_billing(patient_id: str):
    database = get_database()
    events = {event["care_event_id"]: event for event in database.care_events.find(
        {"org_id": DEFAULT_ORG_ID, "patient_id": patient_id_filter(patient_id)})}
    rows = []
    for bill in database.billing_details.find({"org_id": DEFAULT_ORG_ID, "care_event_id": {"$in": list(events)}}):
        event = events[bill["care_event_id"]]
        row = {key: bill.get(key) for key in ("billing_detail_id", "care_event_id", "invoicing_month",
            "sum_covered", "sum_total", "amount_owed", "billing_status", "invoice_number",
            "invoice_created_date", "sent_date", "paid_date")}
        row.update({key: event.get(key, "") for key in ("event_type", "period_start_date", "period_end_date", "care_account")})
        row["services_count"] = len(event.get("services", []))
        row["date_invoice_created"] = bill.get("invoice_created_date")
        row["date_invoice_sent"] = bill.get("sent_date")
        row["date_payment_received"] = bill.get("paid_date")
        row["service_packet_amount"] = bill.get("service_packet_amount", 0)
        row["invoice_total"] = round((bill.get("amount_owed") or 0) + bill.get("service_packet_amount", 0), 2)
        rows.append(row)
    def date_key(row):
        try:
            return parse_service_date(row["period_start_date"]).isoformat()
        except ValueError:
            return ""
    return {"status": "ok", "billing_details": sorted(rows, key=date_key, reverse=True)}


@router.get("/billing/detail/{billing_detail_id}/services")
def get_billing_detail_services(billing_detail_id: str):
    bill = find_bill(billing_detail_id)
    event = get_database().care_events.find_one({"org_id": DEFAULT_ORG_ID, "care_event_id": bill["care_event_id"]})
    if not event:
        raise HTTPException(404, "Care event not found")
    return {"status": "ok", "billing_detail_id": billing_detail_id, "care_event_id": bill["care_event_id"],
            "services": event.get("services", []), "sum_covered": bill.get("sum_covered", 0), "sum_total": bill.get("sum_total", 0)}


@router.put("/billing/detail/{billing_detail_id}/services")
def update_billing_detail_services(billing_detail_id: str, request: ServicesUpdateRequest):
    return update_services(billing_detail_id, request)


@router.post("/billing/detail/{billing_detail_id}/regenerate-pdf")
def regenerate_billing_pdf(billing_detail_id: str):
    from app.invoice_generator import generate_billing_pdf
    generate_billing_pdf(billing_detail_id)
    return {"status": "ok", "message": "PDF generated successfully"}


@router.get("/billing/detail/{billing_detail_id}/pdf")
@router.post("/billing/detail/{billing_detail_id}/view-pdf")
def view_billing_pdf(billing_detail_id: str):
    bill = find_bill(billing_detail_id)
    from app.invoice_generator import OUTPUT_DIR
    root = Path(OUTPUT_DIR).resolve()
    stored = bill.get("pdf_path")
    if not stored:
        number = bill.get("invoice_number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise HTTPException(404, "Generate the invoice PDF first")
        matches = [path for path in root.glob(f"RE_{number}_*.pdf")
                   if path.resolve().parent == root and path.is_file()]
        if len(matches) != 1:
            raise HTTPException(409, "Invoice file is missing or ambiguous; reconcile or regenerate it")
        stored = matches[0].name
    path = (root / stored).resolve()
    if path.parent != root or path.suffix.lower() != ".pdf" or not path.is_file():
        raise HTTPException(404, "Invoice PDF not found")
    return FileResponse(path, media_type="application/pdf", filename=f"RE_{bill['invoice_number']}.pdf",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.patch("/billing/detail/{billing_detail_id}/status")
def update_billing_status(billing_detail_id: str, request: BillingStatusUpdate):
    return transition_status(billing_detail_id, request.billing_status)
