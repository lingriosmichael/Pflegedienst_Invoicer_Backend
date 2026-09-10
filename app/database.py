"""
MongoDB Database Module for Pflegedienst Invoicer

This module provides all database operations using MongoDB exclusively.
Replaces the old SQLite-based database.py.

All functions maintain backward-compatible interfaces while using MongoDB repositories.
"""

import logging
from app.db.transactions import transactional
from app.utils.validation import validate_month, money, parse_service_date
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any
from app.db.mongodb_config import get_database
from app.utils.parsing import GermanDecimalParser, generate_id

# Import MongoDB repositories
from app.db import (
    BillingSummaryRepository,
    PatientRepository,
    InvoiceRepository,
    CareEventRepository,
    ServiceRepository,
    BillingRepository,
    DEFAULT_ORG_ID,
)
from app.utils.data_validation import DataNormalizer, DataValidationError

logger = logging.getLogger(__name__)


def init_db():
    """
    Initialize MongoDB database.
    No-op for MongoDB (collections/indexes created at startup by create_collections_and_indexes).
    """
    logger.info("✓ MongoDB database ready (init_db is no-op for MongoDB)")


def _date_matches_invoicing_month(date_value: Any, invoicing_month: str) -> bool:
    """Check whether a DD.MM.YY or DD.MM.YYYY date falls in MMYYYY."""
    if not date_value or not invoicing_month:
        return False

    date_str = str(date_value).strip()
    mm = invoicing_month[:2]
    yyyy = invoicing_month[2:]
    yy = yyyy[-2:]

    return date_str.endswith(f".{mm}.{yy}") or date_str.endswith(f".{mm}.{yyyy}")


def _care_event_matches_invoicing_month(care_event: Dict[str, Any], invoicing_month: str) -> bool:
    """
    Validate that a care_event actually belongs to the selected month.

    We prefer the care period dates because historical data may contain
    incorrectly assigned invoicing_month values on stored documents.
    """
    if not care_event or not invoicing_month:
        return False

    start_date = care_event.get("period_start_date")
    end_date = care_event.get("period_end_date")

    if start_date or end_date:
        return (
            _date_matches_invoicing_month(start_date, invoicing_month)
            or _date_matches_invoicing_month(end_date, invoicing_month)
        )

    return care_event.get("invoicing_month") == invoicing_month


def _resolve_invoice_amounts(event_type: str, ce: Dict[str, Any], bd: Optional[Dict[str, Any]]) -> Dict[str, float]:
    """
    Normalize invoice financial fields across care_event and billing_details.

    SGBXI PDFs should stay aligned with the embedded service rows on the care_event.
    Entleistung keeps sum_covered editable, so PDF rendering must respect the
    stored covered value instead of reverse-deriving it from amount_owed.
    """
    bd = bd or {}

    ce_sum_total = float(ce.get("sum_total", 0) or 0)
    ce_sum_covered = float(ce.get("sum_covered", 0) or 0)
    bd_sum_total = float(bd.get("sum_total", 0) or 0)
    bd_sum_covered = float(bd.get("sum_covered", 0) or 0)
    bd_amount_owed_raw = bd.get("amount_owed")
    bd_amount_owed = float(bd_amount_owed_raw or 0)
    investitionskosten = float(bd.get("investitionskosten", 0) or 0)

    sum_total = bd_sum_total if "sum_total" in bd else ce_sum_total
    sum_covered = bd_sum_covered if "sum_covered" in bd else ce_sum_covered
    amount_owed = bd_amount_owed

    if event_type == "SGBXI":
        amount_owed = (
            bd_amount_owed
            if bd_amount_owed_raw is not None
            else round(max(sum_total - sum_covered + investitionskosten, 0), 2)
        )
    else:
        if bd_amount_owed_raw is None:
            amount_owed = round(max(sum_total - sum_covered, 0), 2)

    return {
        "sum_total": round(sum_total, 2),
        "sum_covered": round(sum_covered, 2),
        "amount_owed": round(amount_owed, 2),
    }


# ============================================================================
# Billing Summary Functions
# ============================================================================

def insert_billing_summary(data: Dict, abrechnungsmonat: str) -> Optional[str]:
    """
    Insert or aggregate billing summary using MongoDB.
    Accumulates counts/amounts for the same month across multiple PDFs.
    
    Args:
        data: Dictionary with submitted_invoices_count and submitted_invoices_amount
        abrechnungsmonat: Billing month (MMYYYY format)
        
    Returns:
        MongoDB ObjectId as string, or None on error
    """
    try:
        submitted_invoices_count = data.get("submitted_invoices_count", 0)
        submitted_invoices_amount = data.get("submitted_invoices_amount", 0)
        
        if submitted_invoices_count is None or submitted_invoices_amount is None:
            logger.warning("Missing required billing summary fields")
            return None
        
        # Convert German decimal format if needed
        if isinstance(submitted_invoices_amount, str):
            parser = GermanDecimalParser()
            submitted_invoices_amount = parser.parse(submitted_invoices_amount)
        
        # Use MongoDB BillingSummaryRepository (handles upsert with aggregation)
        summary_id = BillingSummaryRepository.insert(
            abrechnungsmonat=abrechnungsmonat,
            submitted_invoices_count=int(submitted_invoices_count),
            submitted_invoices_amount=float(submitted_invoices_amount),
            org_id=DEFAULT_ORG_ID
        )
        
        logger.info(f"✓ Billing summary updated/created: month={abrechnungsmonat}, "
                   f"count={submitted_invoices_count}, amount={submitted_invoices_amount}")
        return summary_id
        
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        return None


def record_file_import(filename: str, abrechnungsmonat: str, import_mode: str, 
                       file_hash: str, invoice_count: int) -> bool:
    """
    Record a file import for tracking and audit purposes.
    
    Args:
        filename: Name of the imported file
        abrechnungsmonat: Billing month
        import_mode: Type of import (sgbxi, sgbv, etc.)
        file_hash: MD5 hash of the file
        invoice_count: Number of invoices imported
        
    Returns:
        True if recording succeeded
    """
    try:
        # Store in care_event_history for audit trail
        db = get_database()
        
        history_doc = {
            "history_id": generate_id("hist"),
            "org_id": DEFAULT_ORG_ID,
            "action": "file_import",
            "filename": filename,
            "abrechnungsmonat": abrechnungsmonat,
            "import_mode": import_mode,
            "file_hash": file_hash,
            "invoice_count": invoice_count,
            "created_at": datetime.utcnow()
        }
        
        db.care_event_history.insert_one(history_doc)
        logger.info(f"Recording file import: {filename}, month={abrechnungsmonat}, "
                   f"mode={import_mode}, invoices={invoice_count}")
        return True
        
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        return False


# ============================================================================
# Structured Data Insertion (PDF Import)
# ============================================================================

def insert_structured_data(data, origin_chunk_id=None, invoicing_month=None):
    from app.import_records import persist_record
    return persist_record(data, origin_chunk_id, invoicing_month)


def insert_care_record(data, record_type, pflegekonto, origin_chunk_id=None, invoicing_month=None):
    from app.import_records import persist_record
    return persist_record(data, origin_chunk_id, invoicing_month, record_type, pflegekonto)


def get_private_invoice_cases(
    invoicing_month: Optional[str] = None,
    invoice_id: Optional[str] = None,
    require_invoice_needed: bool = True,
) -> List[Dict]:
    """
    Get invoices needing private invoices.
    Filters for event_type='SGBXI' or 'Entleistung' (billable types only).
    
    Args:
        invoicing_month: Month to filter (MMYYYY format)
        invoice_id: Specific care_event_id to fetch
        require_invoice_needed: Restrict month queries to billing_status=invoice_needed
        
    Returns:
        List of invoice case dictionaries
    """
    try:
        db = get_database()
        cases = []
        
        if invoice_id:
            # Get specific care event
            ce = db.care_events.find_one({
                "org_id": DEFAULT_ORG_ID,
                "care_event_id": invoice_id,
                "event_type": {"$in": ["SGBXI", "Entleistung", "ServicePacket"]}
            })
            
            if not ce:
                return []
            
            # Get billing details
            bd = db.billing_details.find_one({
                "org_id": DEFAULT_ORG_ID,
                "care_event_id": invoice_id
            })
            
            # Get patient
            patient = db.patient_profiles.find_one({
                "org_id": DEFAULT_ORG_ID,
                "patient_id": ce.get("patient_id")
            })
            
            if patient:
                case = _build_invoice_case(ce, bd, patient)
                if case:
                    cases.append(case)
        else:
            # Get all billable invoices for month
            billing_match = {
                "org_id": DEFAULT_ORG_ID,
                "invoicing_month": invoicing_month,
            }
            if require_invoice_needed:
                billing_match["billing_status"] = "invoice_needed"

            pipeline = [
                {
                    "$match": billing_match
                },
                {
                    "$lookup": {
                        "from": "care_events",
                        "let": {"ce_id": "$care_event_id"},
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {
                                        "$and": [
                                            {"$eq": ["$care_event_id", "$$ce_id"]},
                                            {"$eq": ["$org_id", DEFAULT_ORG_ID]},
                                            {"$in": ["$event_type", ["SGBXI", "Entleistung", "ServicePacket"]]}
                                        ]
                                    }
                                }
                            }
                        ],
                        "as": "care_event"
                    }
                },
                {"$unwind": {"path": "$care_event", "preserveNullAndEmptyArrays": False}},
                {
                    "$lookup": {
                        "from": "patient_profiles",
                        "let": {"patient_id": "$care_event.patient_id"},
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {
                                        "$and": [
                                            {"$eq": ["$patient_id", "$$patient_id"]},
                                            {"$eq": ["$org_id", DEFAULT_ORG_ID]}
                                        ]
                                    }
                                }
                            }
                        ],
                        "as": "patient"
                    }
                },
                {"$unwind": {"path": "$patient", "preserveNullAndEmptyArrays": False}}
            ]
            
            results = list(db.billing_details.aggregate(pipeline))
            
            for doc in results:
                ce = doc.get("care_event", {})
                bd = doc
                patient = doc.get("patient", {})

                if invoicing_month and not _care_event_matches_invoicing_month(ce, invoicing_month):
                    logger.warning(
                        "Skipping billing_detail %s for requested month %s because linked "
                        "care_event %s has period %s to %s",
                        bd.get("billing_detail_id"),
                        invoicing_month,
                        ce.get("care_event_id"),
                        ce.get("period_start_date"),
                        ce.get("period_end_date"),
                    )
                    continue
                
                case = _build_invoice_case(ce, bd, patient)
                if case:
                    cases.append(case)
        
        return cases
        
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        import traceback
        logger.error("Operation failed; transaction will be rolled back")
        return []


def _build_invoice_case(ce: Dict, bd: Optional[Dict], patient: Dict) -> Optional[Dict]:
    """Build an invoice case dictionary from care_event, billing_details, and patient."""
    if not ce or not patient:
        return None
    
    bd = bd or {}
    event_type = ce.get("event_type", "")
    amounts = _resolve_invoice_amounts(event_type, ce, bd)
    
    # Build address from components
    street_name = patient.get("street_name", "") or ""
    street_number = patient.get("street_number", "") or ""
    postal_code = patient.get("postal_code", "") or ""
    city = patient.get("city", "") or ""
    address = f"{street_name} {street_number} {postal_code} {city}".strip()
    
    # Get services from embedded array
    services = ce.get("services", [])
    
    return {
        "invoice": {
            "id": ce.get("care_event_id"),
            "invoicing_month": bd.get("invoicing_month") or ce.get("invoicing_month"),
            "invoice_number": bd.get("invoice_number"),
            "amount_owed": amounts["amount_owed"],
            "investitionskosten": bd.get("investitionskosten", 0),
            "invoice_total": round(amounts["amount_owed"] + bd.get("service_packet_amount", 0), 2),
            "sum_covered": amounts["sum_covered"],
            "sum_total": amounts["sum_total"],
            "care_range_begin": ce.get("period_start_date"),
            "care_range_end": ce.get("period_end_date"),
            "care_account": ce.get("care_account"),
            "event_type": event_type
        },
        "patient": {
            "id": patient.get("patient_id"),
            "name": patient.get("patient_name"),
            "insurance_number": patient.get("insurance_number"),
            "birthdate": patient.get("date_of_birth"),
            "care_level": patient.get("care_level"),
            "include_service_packet": bool(bd.get("service_packet_amount", 0)) if event_type == "SGBXI" else False,
            "address": address,
            "debtor_number": patient.get("debtor_id")
        },
        "services": [
            {
                "id": idx,
                "code": s.get("service_code", ""),
                "description": s.get("service_description", ""),
                "quantity": s.get("quantity_value", 0),
                "unit_price": s.get("unit_price", 0),
                "total_price": s.get("line_total", 0)
            }
            for idx, s in enumerate(services)
        ]
    }


def get_orphaned_service_packet_cases(invoicing_month: str) -> List[Dict]:
    """
    Get patients with include_service_packet=1 but NO SGBXI invoice in the month.
    These patients should still receive an invoice for just the service packet fee.
    
    Args:
        invoicing_month: Month to check (MMYYYY format)
        
    Returns:
        List of synthetic invoice cases with only service packet fee
    """
    try:
        db = get_database()
        cases = []
        
        # Find all patients with service_packet flag
        service_packet_patients = list(db.patient_profiles.find({
            "org_id": DEFAULT_ORG_ID,
            "include_service_packet": 1
        }))
        
        for patient in service_packet_patients:
            patient_id = patient.get("patient_id")

            sgbxi_events = list(db.care_events.find({
                "org_id": DEFAULT_ORG_ID,
                "patient_id": patient_id,
                "event_type": "SGBXI",
            }))

            if any(_care_event_matches_invoicing_month(event, invoicing_month) for event in sgbxi_events):
                # Patient has SGBXI invoice, skip
                continue
            
            # Build address
            street_name = patient.get("street_name", "") or ""
            street_number = patient.get("street_number", "") or ""
            postal_code = patient.get("postal_code", "") or ""
            city = patient.get("city", "") or ""
            address = f"{street_name} {street_number} {postal_code} {city}".strip()
            
            # Create synthetic invoice case
            case = {
                "invoice": {
                    "id": f"service_packet_{patient_id}_{invoicing_month}",
                    "invoicing_month": invoicing_month,
                    "invoice_number": None,
                    "amount_owed": 40.00,
                    "sum_covered": 0.0,
                    "sum_total": 40.00,
                    "care_range_begin": None,
                    "care_range_end": None,
                    "event_type": "ServicePacket"
                },
                "patient": {
                    "id": patient_id,
                    "name": patient.get("patient_name"),
                    "insurance_number": patient.get("insurance_number"),
                    "birthdate": patient.get("date_of_birth"),
                    "care_level": patient.get("care_level"),
                    "include_service_packet": 1,
                    "address": address,
                    "debtor_number": patient.get("debtor_id")
                },
                "services": []
            }
            cases.append(case)
        
        return cases
        
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        return []


# ============================================================================
# Data Validation Functions
# ============================================================================

def check_missing_patient_fields(invoicing_month: str, auto_fix: bool = False) -> None:
    """
    Check for patients missing required fields for billing.
    
    Args:
        invoicing_month: Month to check (MMYYYY format)
        auto_fix: If True, fill with placeholder values
    """
    try:
        db = get_database()
        
        # Find patients with care_events in this month
        pipeline = [
            {
                "$match": {
                    "org_id": DEFAULT_ORG_ID,
                    "invoicing_month": invoicing_month,
                    "event_type": {"$in": ["SGBXI", "Entleistung", "ServicePacket"]}
                }
            },
            {
                "$lookup": {
                    "from": "patient_profiles",
                    "let": {"patient_id": "$patient_id"},
                    "pipeline": [
                        {"$match": {"$expr": {"$eq": ["$patient_id", "$$patient_id"]}}}
                    ],
                    "as": "patient"
                }
            },
            {"$unwind": "$patient"},
            {
                "$match": {
                    "$or": [
                        {"patient.street_name": {"$in": [None, ""]}},
                        {"patient.street_number": {"$in": [None, ""]}},
                        {"patient.postal_code": {"$in": [None, ""]}},
                        {"patient.city": {"$in": [None, ""]}}
                    ]
                }
            },
            {"$group": {"_id": "$patient.patient_id", "patient": {"$first": "$patient"}}}
        ]
        
        patients_missing = list(db.care_events.aggregate(pipeline))
        
        for doc in patients_missing:
            patient = doc.get("patient", {})
            patient_id = patient.get("patient_id")
            name = patient.get("patient_name")
            
            logger.warning("Patient %s has incomplete address fields", patient_id)
            
            if auto_fix:
                update_fields = {}
                if not patient.get("street_name"):
                    update_fields["street_name"] = "[STRASSE ERFORDERLICH]"
                if not patient.get("street_number"):
                    update_fields["street_number"] = "[HAUSNUMMER ERFORDERLICH]"
                if not patient.get("postal_code"):
                    update_fields["postal_code"] = "[PLZ ERFORDERLICH]"
                if not patient.get("city"):
                    update_fields["city"] = "[ORT ERFORDERLICH]"
                
                if update_fields:
                    update_fields["updated_at"] = datetime.utcnow()
                    db.patient_profiles.update_one(
                        {"patient_id": patient_id, "org_id": DEFAULT_ORG_ID},
                        {"$set": update_fields}
                    )
                    logger.info("Patient %s has incomplete address fields", patient_id)
            else:
                logger.warning(f"  - Street Name: {'MISSING' if not patient.get('street_name') else 'OK'}")
                logger.warning(f"  - Street Number: {'MISSING' if not patient.get('street_number') else 'OK'}")
                logger.warning(f"  - Postal Code: {'MISSING' if not patient.get('postal_code') else 'OK'}")
                logger.warning(f"  - City: {'MISSING' if not patient.get('city') else 'OK'}")
                
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)


def check_service_fields(auto_fix: bool = False) -> None:
    """
    Check for services with quantity mismatches.
    
    Args:
        auto_fix: If True, automatically fix mismatches
    """
    try:
        db = get_database()
        
        # Get all care_events with services
        care_events = db.care_events.find({
            "org_id": DEFAULT_ORG_ID,
            "services": {"$exists": True, "$ne": []}
        })
        
        care_events.sort(key=lambda event: (
            str(event.get("patient_id")), parse_service_date(event.get("period_start_date") or f"01.{invoicing_month[:2]}.{invoicing_month[2:]}"),
            str(event.get("care_event_id"))))
        for ce in care_events:
            care_event_id = ce.get("care_event_id")
            services = ce.get("services", [])
            updated_services = []
            needs_update = False
            
            for service in services:
                unit_price = service.get("unit_price", 0)
                line_total = service.get("line_total", 0)
                quantity = service.get("quantity_value", 0)
                
                if not unit_price or not line_total:
                    updated_services.append(service)
                    continue
                
                try:
                    unit_price_float = float(str(unit_price).replace(",", "."))
                    line_total_float = float(str(line_total).replace(",", "."))
                    quantity_float = float(str(quantity).replace(",", "."))
                except ValueError:
                    logger.warning(f"Service in {care_event_id}: non-numeric values, skipping check")
                    updated_services.append(service)
                    continue
                
                if unit_price_float == 0:
                    updated_services.append(service)
                    continue
                
                calculated_quantity = line_total_float / unit_price_float
                percentage_diff = abs(calculated_quantity - quantity_float) / abs(calculated_quantity) * 100 if calculated_quantity != 0 else 0
                
                if percentage_diff < 0.5:
                    updated_services.append(service)
                    continue
                
                logger.warning(f"Service in {care_event_id}: quantity mismatch "
                              f"(expected {calculated_quantity:.2f}, got {quantity_float}).")
                
                if auto_fix:
                    service["quantity_value"] = round(calculated_quantity, 2)
                    logger.info(f"Service in {care_event_id}: auto-fixed quantity to {calculated_quantity:.2f}.")
                    needs_update = True
                
                updated_services.append(service)
            
            if needs_update:
                db.care_events.update_one(
                    {"care_event_id": care_event_id, "org_id": DEFAULT_ORG_ID},
                    {"$set": {"services": updated_services, "updated_at": datetime.utcnow()}}
                )
                
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)


# ============================================================================
# Billing Functions
# ============================================================================

@transactional
def mark_month_ready_for_generation(invoicing_month: str, only_positive: bool = False,
                                     event_types: Optional[List[str]] = None) -> int:
    """
    Create billing_details for care_events that need invoicing.

    SGBXI: Always create billing_details (for investitionskosten)
    Entleistung: Creates a covered-insurance audit marker. RZH is the only
    authority that can later change that one claim to a private invoice.
    SGBV/Verhinderungspflege: Create a not_needed audit marker (non-billable)

    Args:
        invoicing_month: Month to process (MMYYYY format)
        only_positive: If True, only create for positive amounts

    Returns:
        Number of billing_details rows created
    """
    validate_month(invoicing_month)
    try:
        db = get_database()
        billing_rows_created = 0
        
        # The normal invoice workflow must be explicitly scoped to SGB XI.
        # Entlastungsleistung remains importable, but is handled solely by
        # RZH reconciliation and its individual issue action.
        billable_types = event_types or ["SGBXI", "Entleistung", "ServicePacket"]
        care_events = list(db.care_events.find({
            "org_id": DEFAULT_ORG_ID,
            "invoicing_month": invoicing_month,
            "event_type": {"$in": billable_types}
        }))
        
        # Get existing billing_details for this month
        existing_billings = set()
        for bd in db.billing_details.find({"org_id": DEFAULT_ORG_ID}):
            existing_billings.add(bd.get("care_event_id"))
        
        for ce in care_events:
            care_event_id = ce.get("care_event_id")
            patient_id = ce.get("patient_id")
            event_type = ce.get("event_type")

            if not _care_event_matches_invoicing_month(ce, invoicing_month):
                logger.warning(
                    "Skipping care_event %s while marking %s ready because the period %s to %s "
                    "does not belong to that invoicing month",
                    care_event_id,
                    invoicing_month,
                    ce.get("period_start_date"),
                    ce.get("period_end_date"),
                )
                continue
            
            # Skip if already has billing_details
            if care_event_id in existing_billings:
                continue
            
            sum_covered = money(ce.get("sum_covered", 0) or 0)
            sum_total = money(ce.get("sum_total", 0) or 0)
            
            # Calculate investitionskosten (6% for SGBXI only)
            investitionskosten = 0.0
            if event_type == "SGBXI":
                investitionskosten = money(sum_total * 0.06)
            
            # Entlastungsleistung is RZH-authoritative. The annual balance is
            # reference data only and must not calculate, release, or block a
            # patient invoice.
            if event_type == "Entleistung":
                # A service is presumed fully covered when imported.  The
                # source RZH statement can later replace this provisional
                # value with its documented private amount.
                sum_covered = sum_total
                amount_owed = 0.0
            else:
                if sum_covered > sum_total:
                    raise ValueError("Coverage exceeds service total")
                amount_owed = money(sum_total - sum_covered + investitionskosten)

            # Determine if billing should be created
            should_create = False
            billing_status = "covered_insurance"

            if event_type == "SGBXI":
                should_create = True
                billing_status = "invoice_needed"
            elif event_type == "Entleistung":
                # Durable RZH-matchable audit marker; never a normal invoice.
                should_create = True
                billing_status = "covered_insurance"
            elif event_type in {"SGBV", "Verhinderungspflege"}:
                # These care accounts are retained for audit/history, but are
                # never part of the private-invoice workflow.
                should_create = True
                billing_status = "not_needed"
            
            if should_create:
                billing_id = generate_id("bill")
                billing_doc = {
                    "billing_detail_id": billing_id,
                    "org_id": DEFAULT_ORG_ID,
                    "care_event_id": care_event_id,
                    "invoicing_month": invoicing_month,
                    "sum_covered": sum_covered,
                    "sum_total": sum_total,
                    "investitionskosten": investitionskosten,
                    "amount_owed": amount_owed,
                    "invoice_number": None,
                    "billing_status": billing_status,
                    "reconciliation_status": (
                        "assumed_covered_until_rzh" if event_type == "Entleistung" else None
                    ),
                    "coverage_source": (
                        "assumed_full_until_rzh" if event_type == "Entleistung" else None
                    ),
                    "created_at": datetime.utcnow()
                }
                db.billing_details.insert_one(billing_doc)
                billing_rows_created += 1
        
        logger.info("Created %s billing_details rows in %s", billing_rows_created, invoicing_month)
        return billing_rows_created
        
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        import traceback
        logger.error("Operation failed; transaction will be rolled back")
        raise


def _calendar_months_ago(value: datetime, months: int) -> datetime:
    """Return the same wall-clock day N calendar months earlier."""
    absolute_month = value.year * 12 + value.month - 1 - months
    year, month_index = divmod(absolute_month, 12)
    month = month_index + 1
    # All imported rows have a real timestamp. Clamping protects month-end
    # dates if this helper is reused with a different current date.
    import calendar
    return value.replace(year=year, month=month, day=min(value.day, calendar.monthrange(year, month)[1]))


@transactional
def expire_stale_entlastung_coverage(now: Optional[datetime] = None) -> int:
    """Close unchanged Entlastungsleistung claims four calendar months after import.

    An observed negative RZH correction keeps the claim open for review. A
    later correction can still reopen a `not_needed` claim through the normal
    RZH confirmation operation.
    """
    database = get_database()
    now = now or datetime.now(timezone.utc)
    cutoff = _calendar_months_ago(now, 4)
    candidates = list(database.billing_details.find({
        "org_id": DEFAULT_ORG_ID,
        "billing_status": "covered_insurance",
    }))
    changed = 0
    for bill in candidates:
        event = database.care_events.find_one({
            "org_id": DEFAULT_ORG_ID,
            "care_event_id": bill["care_event_id"],
            "event_type": "Entleistung",
        })
        if not event:
            continue
        created_at = event.get("created_at") or bill.get("created_at")
        if not isinstance(created_at, datetime):
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if created_at > cutoff:
            continue
        has_rzh_correction = database.rzh_reconciliation_items.find_one({
            "org_id": DEFAULT_ORG_ID,
            "matched_care_event_id": bill["care_event_id"],
            "section_type": {"$ne": "gutschrift"},
            "amount_cents": {"$lt": 0},
        })
        if has_rzh_correction:
            continue
        database.billing_details.update_one({"_id": bill["_id"]}, {"$set": {
            "billing_status": "not_needed",
            "not_needed_reason": "no_rzh_correction_after_four_months",
            "not_needed_at": now,
            "updated_at": now,
        }})
        database.care_event_history.insert_one({
            "org_id": DEFAULT_ORG_ID,
            "care_event_id": bill["care_event_id"],
            "action": "entlastung_closed_without_rzh_correction",
            "created_at": now,
            "before": {"billing_status": "covered_insurance"},
            "after": {"billing_status": "not_needed"},
        })
        changed += 1
    return changed


@transactional
def validate_and_fix_sgbxi_amounts(care_event_ids=None) -> Dict[str, Any]:
    """
    Report SGBXI events where coverage exceeds the total.
    
    Rule: sum_covered must be <= sum_total (insurance cannot pay more than total)
    
    Returns:
        Dictionary with an explicit list of records requiring review.
    """
    try:
        db = get_database()
        
        # Find SGBXI records where sum_covered > sum_total
        if not care_event_ids:
            raise ValueError("Explicit unissued care event IDs are required")
        reversed_records = list(db.care_events.find({
            "care_event_id": {"$in": care_event_ids},
            "org_id": DEFAULT_ORG_ID,
            "event_type": "SGBXI",
            "$expr": {"$gt": ["$sum_covered", "$sum_total"]}
        }))
        
        # Get total SGBXI count
        total_sgbxi = db.care_events.count_documents({
            "org_id": DEFAULT_ORG_ID,
            "event_type": "SGBXI"
        })
        
        return {
            'corrected_count': 0,
            'total_checked': total_sgbxi,
            'errors': [ce.get("care_event_id") for ce in reversed_records]
        }
        
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        raise
