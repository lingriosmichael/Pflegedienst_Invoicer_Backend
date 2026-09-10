from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import DEFAULT_ORG_ID
from app.db.mongodb_config import get_database
from app.db.transactions import transactional
from app.utils.validation import patient_id_filter, validate_month


def targets(database, query):
    events = list(database.care_events.find(query))
    bills = list(database.billing_details.find({"org_id": query["org_id"],
        "care_event_id": {"$in": [event["care_event_id"] for event in events]}}))
    if any(bill.get("invoice_number") or bill.get("billing_status") in {"sent", "paid"} for bill in bills):
        raise HTTPException(409, "Issued invoice records cannot be deleted; use an accounting correction")
    return events, bills


def delete_records(database, events, bills, org_id):
    event_map = {event["care_event_id"]: event for event in events}
    scope = {"org_id": org_id, "care_event_id": {"$in": list(event_map)}}
    database.billing_details.delete_many(scope)
    database.care_events.delete_many(scope)
    database.care_event_history.delete_many(scope)
    database.service_packet_charges.delete_many({"org_id": org_id,
        "billing_detail_id": {"$in": [bill["billing_detail_id"] for bill in bills]}})
    database.care_event_history.insert_one({"org_id": org_id, "action": "unissued_records_deleted",
        "event_ids": list(event_map), "created_at": datetime.now(timezone.utc)})


@transactional
def delete_patient_records(patient_id):
    database = get_database()
    query = {"org_id": DEFAULT_ORG_ID, "patient_id": patient_id_filter(patient_id)}
    patient = database.patient_profiles.find_one(query)
    if not patient:
        raise HTTPException(404, "Patient not found")
    query["patient_id"] = patient["patient_id"]
    events, bills = targets(database, query)
    delete_records(database, events, bills, DEFAULT_ORG_ID)
    database.entlastung_year_balance.delete_many(query)
    database.entlastungsleistung_tracking.delete_many(query)
    database.patient_profiles.delete_one(query)
    return {"status": "ok", "deleted": True}


@transactional
def delete_month_records(month, dry_run=True, org_id=DEFAULT_ORG_ID):
    validate_month(month)
    database = get_database()
    query = {"org_id": org_id, "invoicing_month": month}
    events, bills = targets(database, query)
    event_ids = [event["care_event_id"] for event in events]
    if database.billing_details.find_one({**query, "care_event_id": {"$nin": event_ids}}):
        raise HTTPException(409, "Month contains orphaned billing rows; reconcile before deletion")
    if not dry_run:
        delete_records(database, events, bills, org_id)
        database.import_jobs.delete_many(query)
        database.billing_summary.delete_many({"org_id": org_id, "abrechnungsmonat": month})
    return {"care_events": len(events), "billing_details": len(bills), "dry_run": dry_run}
