#!/usr/bin/env python3
from app.db.connection import get_database
db = get_database()

# Get sample types
sample_bd = db.billing_details.find_one()
print(f"billing_details.care_event_id: {sample_bd.get('care_event_id')} (type: {type(sample_bd.get('care_event_id')).__name__})")

sample_ce = db.care_events.find_one()
print(f"care_events.care_event_id: {sample_ce.get('care_event_id')} (type: {type(sample_ce.get('care_event_id')).__name__})")
print(f"care_events.patient_id: {sample_ce.get('patient_id')} (type: {type(sample_ce.get('patient_id')).__name__})")

# Get a patient from the billing flow
org_id = "org_default"
pending_ce_ids = db.billing_details.distinct("care_event_id", {"org_id": org_id, "billing_status": "invoice_needed"})
print(f"\nPending care_event_ids: {pending_ce_ids[:5]}...")

patient_ids = db.care_events.distinct("patient_id", {"org_id": org_id, "care_event_id": {"$in": pending_ce_ids}})
print(f"Patient IDs from those events: {patient_ids[:5]}...")

# Test with an actual patient
if patient_ids:
    test_patient_id = patient_ids[0]
    print(f"\nTest patient_id: {test_patient_id} (type: {type(test_patient_id).__name__})")
    ces = list(db.care_events.find({"org_id": org_id, "patient_id": test_patient_id}))
    print(f"Found {len(ces)} care_events")
    if ces:
        ce_ids = [ce["care_event_id"] for ce in ces]
        print(f"care_event_ids: {ce_ids}")
        bds = list(db.billing_details.find({"org_id": org_id, "care_event_id": {"$in": ce_ids}, "billing_status": "invoice_needed"}))
        print(f"Found {len(bds)} billing_details")
