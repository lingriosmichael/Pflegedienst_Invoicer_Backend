from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import DEFAULT_ORG_ID
from app.db.mongodb_config import get_database
from app.db.transactions import transactional
from app.invoice_eligibility import CONFIRMED_ENTLASTUNG_STATUSES
from app.utils.validation import money


@transactional
def update_services(billing_detail_id, request):
    database = get_database()
    identity = {"org_id": DEFAULT_ORG_ID, "billing_detail_id": billing_detail_id}
    bill = database.billing_details.find_one(identity)
    if not bill:
        raise HTTPException(404, "Billing detail not found")
    if bill.get("billing_status") in {"sent", "paid"}:
        raise HTTPException(409, "Issued invoices require an accounting correction")
    if bill.get("reconciliation_status") in CONFIRMED_ENTLASTUNG_STATUSES:
        raise HTTPException(409, "Confirmed RZH settlements require reconciliation review before service edits")
    event = database.care_events.find_one({"org_id": DEFAULT_ORG_ID, "care_event_id": bill["care_event_id"]})
    if not event:
        raise HTTPException(404, "Care event not found")
    if event["event_type"] == "ServicePacket":
        raise HTTPException(409, "Monthly service packet charges cannot be edited as care services")
    now = datetime.now(timezone.utc)
    services = [{**service.model_dump(exclude={"line_total"}),
                 "line_total": money(service.quantity_value * service.unit_price)} for service in request.services]
    total = money(sum(service["line_total"] for service in services))
    event_type = event["event_type"]
    month = bill["invoicing_month"]
    if event_type == "Entleistung":
        # Until an RZH correction is confirmed, Entlastungsleistung is
        # presumed fully covered. This is an audit marker, not a forecast.
        covered = total
    else:
        covered = money(request.sum_covered if request.sum_covered is not None else bill.get("sum_covered", 0))
        if covered > total:
            raise HTTPException(422, "Coverage exceeds the service total")
    investment = money(total * 0.06) if event_type == "SGBXI" else 0.0
    owed = 0.0 if event_type == "Entleistung" else money(max(0, total - covered + investment))
    status = "covered_insurance" if event_type == "Entleistung" else "invoice_needed"
    database.care_events.update_one({"org_id": DEFAULT_ORG_ID, "care_event_id": bill["care_event_id"]},
        {"$set": {"services": services, "sum_total": total, "sum_covered": covered, "updated_at": now}})
    bill_updates = {"sum_total": total, "sum_covered": covered,
        "investitionskosten": investment, "amount_owed": owed, "billing_status": status, "updated_at": now}
    if event_type == "Entleistung":
        bill_updates.update({"reconciliation_status": "assumed_covered_until_rzh",
                             "coverage_source": "assumed_full_until_rzh"})
    database.billing_details.update_one(identity, {"$set": bill_updates,
        "$unset": {"pdf_path": "", "generation_id": "", "invoice_snapshot": ""}})
    database.care_event_history.insert_one({"org_id": DEFAULT_ORG_ID, "care_event_id": bill["care_event_id"],
        "action": "services_updated", "created_at": now,
        "before": {key: bill.get(key) for key in ("sum_total", "sum_covered", "amount_owed", "billing_status")},
        "after": {"sum_total": total, "sum_covered": covered, "amount_owed": owed, "billing_status": status}})
    return {"status": "ok", "sum_total": total, "sum_covered": covered, "amount_owed": owed, "services_count": len(services)}


@transactional
def transition_status(billing_detail_id, status):
    database = get_database()
    identity = {"org_id": DEFAULT_ORG_ID, "billing_detail_id": billing_detail_id}
    bill = database.billing_details.find_one(identity)
    if not bill:
        raise HTTPException(404, "Billing detail not found")
    current = bill.get("billing_status", "")
    if current == status:
        return {"status": "ok", "billing_status": status}
    allowed = {"": {"invoice_needed"}, "invoice_needed": {"sent"}, "sent": {"paid"}, "paid": set(), "covered_insurance": set()}
    if status not in allowed.get(current, set()):
        raise HTTPException(409, "Invalid invoice status transition")
    if status in {"sent", "paid"} and not bill.get("pdf_path"):
        raise HTTPException(409, "Generate the invoice PDF before changing its status")
    updates = {"billing_status": status, "updated_at": datetime.now(timezone.utc)}
    if status in {"sent", "paid"}:
        updates[f"{status}_date"] = updates["updated_at"]
    database.billing_details.update_one(identity, {"$set": updates})
    return {"status": "ok", "billing_status": status}
