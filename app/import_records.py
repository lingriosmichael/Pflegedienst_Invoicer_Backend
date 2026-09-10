from datetime import datetime, timezone

from app.db import DEFAULT_ORG_ID
from app.db.mongodb_config import get_database
from app.db.transactions import transactional
from app.utils.data_validation import DataNormalizer, DataValidationError
from app.utils.parsing import generate_id
from app.utils.validation import money, validate_month, parse_service_date


@transactional
def persist_record(data, origin_chunk_id, invoicing_month, record_type=None, care_account=None):
    database = get_database()
    validate_month(invoicing_month)
    if not origin_chunk_id:
        raise ValueError("Stable source record identity is required")
    identity = {"org_id": DEFAULT_ORG_ID, "origin_chunk_id": origin_chunk_id, "invoicing_month": invoicing_month}
    existing = database.care_events.find_one(identity)
    if existing:
        return {"care_event_id": existing["care_event_id"], "duplicate": True}
    patient = data["patient"]
    invoice = data["invoice"]
    if not patient.get("name", "").strip() or not patient.get("insurance_number", "").strip():
        raise ValueError("Patient identity is incomplete")
    patient_name = DataNormalizer.normalize_patient_name(patient["name"])
    # A missing/unparseable birthdate is a known RZH source-data gap (some
    # patients' lines simply omit it). It should not block the record from
    # being created -- flag it for manual patching instead of failing import.
    review_reasons = []
    try:
        birthdate = DataNormalizer.normalize_birthdate(patient.get("birthdate"))
        parse_service_date(birthdate)
    except (DataValidationError, ValueError):
        birthdate = ""
        review_reasons.append("missing_or_invalid_birthdate")
    total = money(DataNormalizer.normalize_amount(invoice["summe_total"]))
    covered = money(DataNormalizer.normalize_amount(invoice["summe_covered"]))
    if covered > total:
        raise ValueError("Coverage exceeds service total; review source amounts")
    start = invoice.get("pflegezeitraum_beginn")
    end = invoice.get("pflegezeitraum_ende")
    if parse_service_date(start) > parse_service_date(end):
        raise ValueError("Service period is reversed")
    account = str(care_account or invoice.get("care_account") or patient.get("pflege_konto", "")).strip()
    event_type = record_type or {"4062": "Consultation", "4064": "Entleistung", "4050": "Verhinderungspflege", "4092": "SGBV", "4010": "SGBXI", "4020": "SGBXI", "4030": "SGBXI", "4040": "SGBXI"}.get(account)
    if not event_type:
        raise ValueError("Unsupported care account")
    services = []
    for service in data["services"]:
        services.append({
            "service_code": service["code"], "service_description": service["description"],
            "quantity_value": float(DataNormalizer.normalize_amount(service["quantity"])),
            "unit_price": money(DataNormalizer.normalize_amount(service["unit_price"])),
            "line_total": money(DataNormalizer.normalize_amount(service["total_price"])),
        })
    if not services or abs(sum(service["line_total"] for service in services) - total) > 0.01:
        raise ValueError("Service lines do not reconcile to invoice total")
    now = datetime.now(timezone.utc)
    patient_query = {"org_id": DEFAULT_ORG_ID, "insurance_number": patient["insurance_number"]}
    profile = database.patient_profiles.find_one(patient_query)
    patient_id = profile["patient_id"] if profile else generate_id("pat")
    database.patient_profiles.update_one(patient_query, {"$set": {
        "patient_name": patient_name, "date_of_birth": birthdate,
        "care_level": patient.get("care_level", ""), "updated_at": now,
        # Re-set (not merged) every import so a later corrected birthdate
        # clears the flag automatically instead of needing a manual unset.
        "needs_review": bool(review_reasons), "review_reasons": review_reasons,
    }, "$setOnInsert": {"patient_id": patient_id, "include_service_packet": False, "created_at": now}}, upsert=True)
    event_id = generate_id("evt")
    database.care_events.insert_one({**identity, "care_event_id": event_id, "patient_id": patient_id,
        "import_identity_version": 1,
        "event_type": event_type, "care_account": account, "period_start_date": start,
        "period_end_date": end, "sum_total": total, "sum_covered": covered,
        "services": services, "created_at": now,
        "needs_review": bool(review_reasons), "review_reasons": review_reasons})
    database.care_event_history.insert_one({"org_id": DEFAULT_ORG_ID, "history_id": generate_id("hist"),
        "care_event_id": event_id, "action": "created", "created_at": now})
    return {"care_event_id": event_id, "duplicate": False}
