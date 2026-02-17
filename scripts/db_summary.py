#!/usr/bin/env python3
import sys
sys.path.insert(0, "/Users/michaelfernandolingrios/Documents/Invoicer/pflegedienst_invoicer")
from app.db.connection import get_database

db = get_database()

print("=== COLLECTIONS ===")
for c in sorted(db.list_collection_names()):
    print(f"  {c}: {db[c].count_documents({})} docs")

print("\n=== CARE EVENTS BY TYPE ===")
for et in sorted(db.care_events.distinct("event_type", {"org_id": "org_default"})):
    cnt = db.care_events.count_documents({"org_id": "org_default", "event_type": et})
    # Sum amounts - field is sum_total
    total = 0
    for doc in db.care_events.find({"org_id": "org_default", "event_type": et}):
        amt = doc.get("sum_total", 0)
        if isinstance(amt, str):
            try:
                amt = float(amt.replace(",", ".").replace("€", "").strip())
            except:
                amt = 0
        total += amt or 0
    print(f"  {et}: {cnt} records, EUR {total:,.2f}")

print(f"\n=== PATIENTS ===")
# Check both collections
patients_count = db.patients.count_documents({"org_id": "org_default"})
profiles_count = db.patient_profiles.count_documents({})
print(f"  patients collection: {patients_count}")
print(f"  patient_profiles collection: {profiles_count}")

# Date range
agg = list(db.care_events.aggregate([
    {"$match": {"org_id": "org_default"}},
    {"$group": {"_id": None, "min": {"$min": "$period_start_date"}, "max": {"$max": "$period_start_date"}}}
]))
if agg:
    print(f"\n=== DATE RANGE ===")
    print(f"  From: {agg[0]['min']}")
    print(f"  To: {agg[0]['max']}")
