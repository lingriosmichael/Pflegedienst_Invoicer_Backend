import hashlib
import json
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import DEFAULT_ORG_ID, InvoiceRepository
from app.db.mongodb_config import get_database
from app.db.transactions import transactional
from app.invoice_eligibility import assert_invoice_eligible
from app.utils.parsing import generate_id
from app.utils.validation import validate_month


@transactional
def ensure_service_packets(month):
    validate_month(month)
    database = get_database()
    for patient in database.patient_profiles.find({"org_id": DEFAULT_ORG_ID, "include_service_packet": {"$in": [True, 1]}}):
        identity = {"org_id": DEFAULT_ORG_ID, "patient_id": patient["patient_id"], "invoicing_month": month}
        if database.service_packet_charges.find_one(identity):
            continue
        events = list(database.care_events.find({**identity, "event_type": "SGBXI"}).sort("care_event_id", 1))
        bills = list(database.billing_details.find({"org_id": DEFAULT_ORG_ID,
            "care_event_id": {"$in": [event["care_event_id"] for event in events]}, "invoicing_month": month}).sort("care_event_id", 1))
        if any(bill.get("invoice_number") for bill in bills):
            raise HTTPException(409, "Existing packet invoices require reconciliation before another monthly charge")
        if bills:
            bill_id = bills[0]["billing_detail_id"]
            database.billing_details.update_one({"org_id": DEFAULT_ORG_ID, "billing_detail_id": bill_id},
                {"$set": {"service_packet_amount": 40.0}})
        else:
            event_id, bill_id = generate_id("evt"), generate_id("bill")
            now = datetime.now(timezone.utc)
            database.care_events.insert_one({**identity, "care_event_id": event_id, "event_type": "ServicePacket",
                "period_start_date": f"01.{month[:2]}.{month[2:]}", "period_end_date": f"01.{month[:2]}.{month[2:]}",
                "sum_total": 40.0, "sum_covered": 0.0, "services": [], "created_at": now})
            database.billing_details.insert_one({"org_id": DEFAULT_ORG_ID, "billing_detail_id": bill_id,
                "care_event_id": event_id, "invoicing_month": month, "sum_total": 40.0, "sum_covered": 0.0,
                "amount_owed": 40.0, "investitionskosten": 0.0, "service_packet_amount": 0.0,
                "invoice_number": None, "billing_status": "invoice_needed", "created_at": now})
        database.service_packet_charges.insert_one({**identity, "billing_detail_id": bill_id, "amount": 40.0})


@transactional
def prepare_invoice(billing_detail_id):
    from app.database import _build_invoice_case

    database = get_database()
    identity = {"org_id": DEFAULT_ORG_ID, "billing_detail_id": billing_detail_id}
    bill = database.billing_details.find_one(identity)
    if not bill:
        raise HTTPException(404, "Billing detail not found")
    event = database.care_events.find_one({"org_id": DEFAULT_ORG_ID, "care_event_id": bill["care_event_id"]})
    if not event:
        raise HTTPException(404, "Care event not found")

    # The €40 service packet belongs exclusively to the SGB XI monthly
    # workflow.  RZH-confirmed Entlastungsleistung invoices are issued one at
    # a time and must not reconcile or create packet charges for their month.
    if event.get("event_type") == "SGBXI":
        ensure_service_packets(bill["invoicing_month"])
        bill = database.billing_details.find_one(identity)
        if not bill:
            raise HTTPException(404, "Billing detail not found")
    if bill.get("billing_status") in {"sent", "paid"}:
        if not bill.get("invoice_snapshot"):
            raise HTTPException(409, "Historical issued invoice requires a preserved snapshot")
        return bill["invoice_snapshot"]
    assert_invoice_eligible(bill, event)
    patient = database.patient_profiles.find_one({"org_id": DEFAULT_ORG_ID, "patient_id": event["patient_id"]})
    if not patient:
        raise HTTPException(404, "Patient not found")
    if not bill.get("invoice_number"):
        bill["invoice_number"] = InvoiceRepository.get_next_invoice_number()
    case = _build_invoice_case(event, bill, patient)
    generation_id = hashlib.sha256(json.dumps(case, sort_keys=True, default=str).encode()).hexdigest()
    case["generation_id"] = generation_id
    database.billing_details.update_one(identity, {"$set": {"invoice_number": bill["invoice_number"],
        "generation_id": generation_id, "invoice_snapshot": case, "invoice_total": round(bill["amount_owed"] + bill.get("service_packet_amount", 0), 2)}})
    return case
