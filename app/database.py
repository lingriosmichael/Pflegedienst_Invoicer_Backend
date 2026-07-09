"""
MongoDB Database Module for Pflegedienst Invoicer

This module provides all database operations using MongoDB exclusively.
Replaces the old SQLite-based database.py.

All functions maintain backward-compatible interfaces while using MongoDB repositories.
"""

import logging
from datetime import datetime
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

    sum_total = ce_sum_total if ce_sum_total > 0 else bd_sum_total
    sum_covered = ce_sum_covered if ce_sum_covered > 0 else bd_sum_covered
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
        logger.error(f"Error inserting billing summary: {e}")
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
        logger.error(f"Error recording file import: {e}")
        return False


# ============================================================================
# Structured Data Insertion (PDF Import)
# ============================================================================

def insert_structured_data(data: Dict, origin_chunk_id: Optional[str] = None, 
                           invoicing_month: Optional[str] = None) -> Optional[str]:
    """
    Insert invoice data into MongoDB.
    Maps to: patient_profiles, care_events (with embedded services)
    
    Args:
        data: Structured invoice data with patient, invoice, services keys
        origin_chunk_id: Optional chunk ID for tracking
        invoicing_month: Billing month (MMYYYY format)
        
    Returns:
        care_event_id on success, None on error
    """
    try:
        db = get_database()
        
        patient = data["patient"]
        invoice = data["invoice"]
        services = data["services"]

        if "care_account" not in invoice:
            invoice["care_account"] = patient.get("pflege_konto", "")

        # Normalize birthdate
        try:
            patient["birthdate"] = DataNormalizer.normalize_birthdate(patient.get("birthdate", ""))
        except DataValidationError as e:
            logger.error(f"❌ Failed to normalize birthdate for patient {patient.get('name', '[unknown]')}: {e}")
            return None

        # Normalize invoice amounts
        try:
            covered_float = float(DataNormalizer.normalize_amount(invoice.get("summe_covered", "0")))
            total_float = float(DataNormalizer.normalize_amount(invoice.get("summe_total", "0")))
            invoice["summe_covered"] = covered_float
            invoice["summe_total"] = total_float
        except DataValidationError as e:
            logger.error(f"❌ Failed to normalize invoice amounts for patient {patient.get('name', '[unknown]')}: {e}")
            return None

        amount_owed = total_float - covered_float

        # Normalize service amounts
        normalized_services = []
        for s in services:
            try:
                unit_price = DataNormalizer.normalize_amount(s.get("unit_price", "0"))
                total_price = DataNormalizer.normalize_amount(s.get("total_price", "0"))
                quantity_str = str(s.get("quantity", "0")).strip().replace(',', '.')
                
                normalized_services.append({
                    "service_code": s.get("code", ""),
                    "service_description": s.get("description", ""),
                    "quantity_value": float(quantity_str) if quantity_str else 0,
                    "unit_price": float(unit_price),
                    "line_total": float(total_price)
                })
            except (DataValidationError, ValueError) as e:
                logger.warning(f"Failed to normalize service: {e}")

        # Generate IDs
        care_event_id = generate_id("evt")
        
        # Check if patient exists by insurance_number
        existing_patient = db.patient_profiles.find_one({
            "org_id": DEFAULT_ORG_ID,
            "insurance_number": patient.get("insurance_number")
        })
        
        if existing_patient:
            patient_id = existing_patient["patient_id"]
            # Update patient with new data
            update_fields = {
                "patient_name": patient.get("name"),
                "date_of_birth": patient.get("birthdate"),
                "updated_at": datetime.utcnow()
            }
            if patient.get("care_level"):
                update_fields["care_level"] = patient.get("care_level")
            
            db.patient_profiles.update_one(
                {"patient_id": patient_id, "org_id": DEFAULT_ORG_ID},
                {"$set": update_fields}
            )
        else:
            # Create new patient
            patient_id = generate_id("pat")
            patient_doc = {
                "patient_id": patient_id,
                "org_id": DEFAULT_ORG_ID,
                "patient_name": patient.get("name"),
                "date_of_birth": patient.get("birthdate"),
                "insurance_number": patient.get("insurance_number"),
                "care_level": patient.get("care_level"),
                "include_service_packet": int(bool(patient.get("include_service_packet", 0))),
                "created_at": datetime.utcnow()
            }
            db.patient_profiles.insert_one(patient_doc)

        # Determine event_type based on care_account
        care_account = invoice.get("care_account", "")
        if care_account == "4062":
            event_type = "Consultation"
        elif care_account == "4064":
            event_type = "Entleistung"
        elif care_account == "4050":
            event_type = "Verhinderungspflege"
        elif care_account in ["4010", "4020", "4030", "4040"]:
            event_type = "SGBXI"
        else:
            event_type = "SGBV"

        logger.info(f"→ Inserting care_event for {patient.get('name')} "
                   f"(insurance: {patient.get('insurance_number')}, care_account: {care_account}, event_type: {event_type})")

        # Create care_event with embedded services
        care_event_doc = {
            "care_event_id": care_event_id,
            "org_id": DEFAULT_ORG_ID,
            "patient_id": patient_id,
            "event_type": event_type,
            "period_start_date": invoice.get("pflegezeitraum_beginn"),
            "period_end_date": invoice.get("pflegezeitraum_ende"),
            "care_account": care_account,
            "sum_covered": covered_float,
            "sum_total": total_float,
            "invoicing_month": invoicing_month,
            "services": normalized_services,
            "origin_chunk_id": origin_chunk_id,
            "created_at": datetime.utcnow()
        }
        db.care_events.insert_one(care_event_doc)

        # Create history record
        history_doc = {
            "history_id": generate_id("hist"),
            "org_id": DEFAULT_ORG_ID,
            "care_event_id": care_event_id,
            "action": "created",
            "created_at": datetime.utcnow()
        }
        db.care_event_history.insert_one(history_doc)

        logger.info(f"✓ Care event inserted: {care_event_id}, type={event_type}, patient={patient.get('name')}")
        return care_event_id

    except Exception as e:
        logger.error(f"Error inserting structured data: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


def insert_care_record(data: Dict, record_type: str, pflegekonto: str, 
                       origin_chunk_id: Optional[str] = None) -> Optional[str]:
    """
    Insert a non-billable care record (SGBV, Verhinderungspflege).
    
    Args:
        data: Dictionary with patient, services, invoice keys
        record_type: 'SGBV' or 'Verhinderungspflege'
        pflegekonto: Care account code (4092 or 4050)
        origin_chunk_id: Optional chunk ID for traceability
        
    Returns:
        care_event_id on success, None on error
    """
    try:
        db = get_database()
        
        patient = data.get("patient", {})
        invoice = data.get("invoice", {})
        services = data.get("services", [])
        
        # Normalize birthdate
        try:
            patient["birthdate"] = DataNormalizer.normalize_birthdate(patient.get("birthdate", ""))
        except DataValidationError as e:
            logger.error(f"Failed to normalize birthdate: {e}")
            return None
        
        # Normalize service amounts
        normalized_services = []
        for service in services:
            try:
                unit_price = float(DataNormalizer.normalize_amount(service.get("unit_price", "0")))
                total_price = float(DataNormalizer.normalize_amount(service.get("total_price", "0")))
                quantity_str = str(service.get("quantity", "0")).strip().replace(',', '.')
                
                normalized_services.append({
                    "service_code": service.get("code", ""),
                    "service_description": service.get("description", ""),
                    "quantity_value": float(quantity_str) if quantity_str else 0,
                    "unit_price": unit_price,
                    "line_total": total_price
                })
            except (DataValidationError, ValueError) as e:
                logger.warning(f"Failed to normalize service in care record: {e}")
        
        # Check if patient exists
        existing_patient = db.patient_profiles.find_one({
            "org_id": DEFAULT_ORG_ID,
            "insurance_number": patient.get("insurance_number")
        })
        
        if existing_patient:
            patient_id = existing_patient["patient_id"]
            update_fields = {
                "patient_name": patient.get("name"),
                "date_of_birth": patient.get("birthdate"),
                "updated_at": datetime.utcnow()
            }
            if patient.get("care_level"):
                update_fields["care_level"] = patient.get("care_level")
            
            db.patient_profiles.update_one(
                {"patient_id": patient_id, "org_id": DEFAULT_ORG_ID},
                {"$set": update_fields}
            )
        else:
            patient_id = generate_id("pat")
            patient_doc = {
                "patient_id": patient_id,
                "org_id": DEFAULT_ORG_ID,
                "patient_name": patient.get("name"),
                "date_of_birth": patient.get("birthdate"),
                "insurance_number": patient.get("insurance_number"),
                "care_level": patient.get("care_level"),
                "created_at": datetime.utcnow()
            }
            db.patient_profiles.insert_one(patient_doc)
        
        # Normalize invoice amounts
        try:
            covered_float = float(DataNormalizer.normalize_amount(invoice.get("summe_covered", "0")))
            total_float = float(DataNormalizer.normalize_amount(invoice.get("summe_total", "0")))
        except DataValidationError:
            covered_float = 0.0
            total_float = 0.0
        
        # Create care event
        care_event_id = generate_id("evt")
        care_event_doc = {
            "care_event_id": care_event_id,
            "org_id": DEFAULT_ORG_ID,
            "patient_id": patient_id,
            "event_type": record_type,
            "period_start_date": invoice.get("pflegezeitraum_beginn"),
            "period_end_date": invoice.get("pflegezeitraum_ende"),
            "care_account": pflegekonto,
            "sum_covered": covered_float,
            "sum_total": total_float,
            "services": normalized_services,
            "origin_chunk_id": origin_chunk_id,
            "created_at": datetime.utcnow()
        }
        db.care_events.insert_one(care_event_doc)
        
        # Create history record
        history_doc = {
            "history_id": generate_id("hist"),
            "org_id": DEFAULT_ORG_ID,
            "care_event_id": care_event_id,
            "action": "created",
            "created_at": datetime.utcnow()
        }
        db.care_event_history.insert_one(history_doc)
        
        logger.info(f"✓ Care event inserted: {care_event_id}, type={record_type}, "
                   f"patient={patient.get('name')}, services={len(normalized_services)}")
        return care_event_id

    except Exception as e:
        logger.error(f"Error inserting care record: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


# ============================================================================
# Invoice Generation Functions
# ============================================================================

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
                "event_type": {"$in": ["SGBXI", "Entleistung"]}
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
                                            {"$in": ["$event_type", ["SGBXI", "Entleistung"]]}
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
        logger.error(f"Error getting private invoice cases: {e}")
        import traceback
        logger.error(traceback.format_exc())
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
            "include_service_packet": patient.get("include_service_packet", 0) if event_type == "SGBXI" else 0,
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
        logger.error(f"Error getting orphaned service packet cases: {e}")
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
                    "event_type": {"$in": ["SGBXI", "Entleistung"]}
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
            
            logger.warning(f"Patient '{name}' ({patient.get('insurance_number')}) is missing address fields.")
            
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
                    logger.info(f"Patient {name}: auto-fixed with placeholder values.")
            else:
                logger.warning(f"  - Street Name: {'MISSING' if not patient.get('street_name') else 'OK'}")
                logger.warning(f"  - Street Number: {'MISSING' if not patient.get('street_number') else 'OK'}")
                logger.warning(f"  - Postal Code: {'MISSING' if not patient.get('postal_code') else 'OK'}")
                logger.warning(f"  - City: {'MISSING' if not patient.get('city') else 'OK'}")
                
    except Exception as e:
        logger.error(f"Error checking missing patient fields: {e}")


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
        logger.error(f"Error checking service fields: {e}")


# ============================================================================
# Billing Functions
# ============================================================================

def mark_month_ready_for_generation(invoicing_month: str, only_positive: bool = False, 
                                     legacy_entleistung: bool = False) -> int:
    """
    Create billing_details for care_events that need invoicing.
    
    SGBXI: Always create billing_details (for investitionskosten)
    Entleistung: Create if sum_total > 127.35 EUR (legacy) or cumulative > 1500 EUR/year (new)
    SGBV/Verhinderungspflege/Consultation: Never create (non-billable)
    
    Args:
        invoicing_month: Month to process (MMYYYY format)
        only_positive: If True, only create for positive amounts
        legacy_entleistung: If True, use simple monthly threshold logic
        
    Returns:
        Number of billing_details rows created
    """
    try:
        db = get_database()
        billing_rows_created = 0
        
        # Find billable care_events without billing_details
        care_events = list(db.care_events.find({
            "org_id": DEFAULT_ORG_ID,
            "invoicing_month": invoicing_month,
            "event_type": {"$in": ["SGBXI", "Entleistung"]}
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
            
            sum_covered = ce.get("sum_covered", 0) or 0
            sum_total = ce.get("sum_total", 0) or 0
            
            # Calculate investitionskosten (6% for SGBXI only)
            investitionskosten = 0.0
            if event_type == "SGBXI":
                investitionskosten = sum_total * 0.06
            
            # Entleistung special handling
            if event_type == "Entleistung":
                ENTLEISTUNG_CAP = 127.35
                sum_covered = min(sum_total, ENTLEISTUNG_CAP)
                amount_owed = sum_total - sum_covered
            else:
                amount_owed = sum_total - sum_covered + investitionskosten
            
            # Determine if billing should be created
            should_create = False
            billing_status = "covered_insurance"
            
            if event_type == "SGBXI":
                should_create = True
                billing_status = "invoice_needed"
            elif event_type == "Entleistung":
                if legacy_entleistung:
                    # Legacy: simple monthly threshold
                    if sum_total > 127.35:
                        should_create = True
                        billing_status = "invoice_needed"
                else:
                    # New: cumulative yearly limit (1500 EUR)
                    year = int(invoicing_month[-4:]) if len(invoicing_month) >= 6 else datetime.now().year
                    
                    # Get cumulative for this patient/year
                    existing_events = db.care_events.find({
                        "org_id": DEFAULT_ORG_ID,
                        "patient_id": patient_id,
                        "event_type": "Entleistung",
                        "invoicing_month": {"$regex": f".*{year}$"}
                    })
                    
                    cumulative = sum(e.get("sum_total", 0) for e in existing_events)
                    
                    if cumulative > 1500.0:
                        should_create = True
                        billing_status = "invoice_needed"
            
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
                    "created_at": datetime.utcnow()
                }
                db.billing_details.insert_one(billing_doc)
                billing_rows_created += 1
        
        entleistung_logic = "legacy (>127.35 EUR/month)" if legacy_entleistung else "new (yearly cumulative 1500 EUR)"
        logger.info(f"Created {billing_rows_created} billing_details rows in {invoicing_month}: "
                   f"SGBXI=all, Entleistung={entleistung_logic}")
        return billing_rows_created
        
    except Exception as e:
        logger.error(f"Error marking month ready for generation: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return 0


def validate_and_fix_sgbxi_amounts() -> Dict[str, Any]:
    """
    Validate SGBXI care_events and auto-correct reversed sum_covered/sum_total.
    
    Rule: sum_covered must be <= sum_total (insurance cannot pay more than total)
    
    Returns:
        Dictionary with corrected_count, total_checked, errors
    """
    try:
        db = get_database()
        
        # Find SGBXI records where sum_covered > sum_total
        reversed_records = list(db.care_events.find({
            "org_id": DEFAULT_ORG_ID,
            "event_type": "SGBXI",
            "$expr": {"$gt": ["$sum_covered", "$sum_total"]}
        }))
        
        corrected_count = 0
        
        for ce in reversed_records:
            care_event_id = ce.get("care_event_id")
            sum_covered = ce.get("sum_covered", 0)
            sum_total = ce.get("sum_total", 0)
            corrected_sum_covered = sum_total
            corrected_sum_total = sum_covered
            
            # Swap them
            db.care_events.update_one(
                {"care_event_id": care_event_id, "org_id": DEFAULT_ORG_ID},
                {
                    "$set": {
                        "sum_covered": corrected_sum_covered,
                        "sum_total": corrected_sum_total,
                        "updated_at": datetime.utcnow()
                    }
                }
            )

            # Keep billing_details in sync with the corrected care_event totals.
            investitionskosten = corrected_sum_total * 0.06
            amount_owed = corrected_sum_total - corrected_sum_covered + investitionskosten
            db.billing_details.update_many(
                {"care_event_id": care_event_id, "org_id": DEFAULT_ORG_ID},
                {
                    "$set": {
                        "sum_covered": corrected_sum_covered,
                        "sum_total": corrected_sum_total,
                        "investitionskosten": investitionskosten,
                        "amount_owed": amount_owed,
                        "updated_at": datetime.utcnow()
                    }
                }
            )
            
            logger.warning(f"✓ AUTO-CORRECTED SGBXI {care_event_id}: "
                          f"swapped sum_covered={sum_covered:.2f} <-> sum_total={sum_total:.2f}")
            corrected_count += 1
        
        # Get total SGBXI count
        total_sgbxi = db.care_events.count_documents({
            "org_id": DEFAULT_ORG_ID,
            "event_type": "SGBXI"
        })
        
        return {
            'corrected_count': corrected_count,
            'total_checked': total_sgbxi,
            'errors': []
        }
        
    except Exception as e:
        logger.error(f"Error validating SGBXI amounts: {e}")
        return {
            'corrected_count': 0,
            'total_checked': 0,
            'errors': [str(e)]
        }
