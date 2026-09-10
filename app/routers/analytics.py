import traceback
from app.utils.validation import parse_service_date, month_sort_key
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()

GERMAN_MONTHS = {
    "01": "Januar", "02": "Februar", "03": "März", "04": "April",
    "05": "Mai", "06": "Juni", "07": "Juli", "08": "August",
    "09": "September", "10": "Oktober", "11": "November", "12": "Dezember",
}

class PatientHistogramRequest(BaseModel):
    patient_id: int

@router.get("/analytics/patients")
def get_patients():
    """Get list of all patients for dashboard sidebar."""
    try:
        from app.chart_generator import get_all_patients
        
        patients = get_all_patients()
        
        return {
            "status": "ok",
            "patients": patients
        }
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Operation failed; verify input and database readiness")

@router.get("/analytics/patient/{patient_id}/histogram")
def get_patient_histogram(patient_id: str):
    """Generate histogram of invoice amounts by month for a patient."""
    try:
        from app.chart_generator import generate_patient_histogram
        
        chart_data = generate_patient_histogram(patient_id)
        
        return {
            "status": "ok",
            "chart_data": chart_data
        }
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Operation failed; verify input and database readiness")


@router.get("/analytics/billing-summary")
def get_billing_summary():
    """Get billing summary data by care type (SGBXI, Entleistung, SGBV, Verhinderungspflege) grouped by month for stacked bar chart using MongoDB aggregation."""
    try:
        from app.db.connection import get_database
        from datetime import datetime
        
        db = get_database()
        org_id = "org_default"
        
        # Aggregate by both period_start_date and event_type
        pipeline = [
            {"$match": {"org_id": org_id}},
            {
                "$group": {
                    "_id": {
                        "date": "$period_start_date",
                        "event_type": "$event_type"
                    },
                    "total_amount": {"$sum": {"$toDouble": "$sum_total"}},
                    "record_count": {"$sum": 1}
                }
            },
            {"$sort": {"_id.date": 1}}
        ]
        
        results = list(db.care_events.aggregate(pipeline))
        
        # Build monthly data with event types as columns
        month_data = {}
        years_present = set()
        
        for item in results:
            date_str = item.get("_id", {}).get("date", "")
            event_type = item.get("_id", {}).get("event_type", "Unknown")
            total_amount = item.get("total_amount", 0)
            
            if date_str:
                try:
                    # Parse date - handle both DD.MM.YY and DD.MM.YYYY formats
                    date_str = str(date_str).strip()
                    try:
                        dt = parse_service_date(date_str)
                    except ValueError:
                        dt = datetime.strptime(date_str, "%d.%m.%Y")
                    
                    month_key = dt.strftime("%m%Y")
                    month_display = dt.strftime("%m/%Y")
                    year = dt.strftime("%Y")
                    years_present.add(year)
                    
                    if month_key not in month_data:
                        month_data[month_key] = {
                            "month": month_display,
                            "year": year,
                            "SGBXI": 0.0,
                            "Entleistung": 0.0,
                            "SGB V": 0.0,
                            "Verhinderungspflege": 0.0,
                            "Beratungsbesuche": 0.0,
                        }
                    
                    # Map event types to columns
                    if event_type == "SGBXI":
                        month_data[month_key]["SGBXI"] += float(total_amount) if total_amount else 0.0
                    elif event_type == "Entleistung":
                        month_data[month_key]["Entleistung"] += float(total_amount) if total_amount else 0.0
                    elif event_type == "SGBV":  # Note: database has SGBV without space
                        month_data[month_key]["SGB V"] += float(total_amount) if total_amount else 0.0
                    elif event_type == "Verhinderungspflege":
                        month_data[month_key]["Verhinderungspflege"] += float(total_amount) if total_amount else 0.0
                    elif event_type == "Consultation":  # Consultation = Beratungsbesuche
                        month_data[month_key]["Beratungsbesuche"] += float(total_amount) if total_amount else 0.0
                        
                except (ValueError, TypeError) as pe:
                    logger.warning(f"Could not parse date: {date_str}, error: {pe}")
        
        # Fill in all 12 months for each year present
        if years_present:
            for year in sorted(years_present):
                for month_num in range(1, 13):
                    month_key = f"{month_num:02d}{year}"
                    if month_key not in month_data:
                        month_display = f"{month_num:02d}/{year}"
                        month_data[month_key] = {
                            "month": month_display,
                            "year": year,
                            "SGBXI": 0.0,
                            "Entleistung": 0.0,
                            "SGB V": 0.0,
                            "Verhinderungspflege": 0.0,
                            "Beratungsbesuche": 0.0,
                        }
        
        # Convert to sorted list, round values
        data = sorted(month_data.values(), key=lambda x: month_sort_key(x["month"]))
        for row in data:
            row["SGBXI"] = round(row["SGBXI"], 2)
            row["Entleistung"] = round(row["Entleistung"], 2)
            row["SGB V"] = round(row["SGB V"], 2)
            row["Verhinderungspflege"] = round(row["Verhinderungspflege"], 2)
            row["Beratungsbesuche"] = round(row["Beratungsbesuche"], 2)
        
        # Extract unique years for selection
        years = sorted(list(years_present))
        
        return {
            "status": "ok",
            "years": years,
            "data": data
        }
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Operation failed; verify input and database readiness")


def _format_date_grouped_analytics(aggregation_results):
    """
    Helper function to format MongoDB aggregation results for analytics endpoints.
    
    Converts aggregation results with _id (date) to monthly data with month display.
    
    Args:
        aggregation_results: List of dicts from MongoDB aggregation with _id, record_count, total_amount
        
    Returns:
        List of dicts with month display and aggregated data
    """
    from datetime import datetime
    
    month_data = {}
    years_present = set()
    
    for result in aggregation_results:
        date_str = result.get("_id", "")
        record_count = result.get("record_count", 0)
        total_amount = result.get("total_amount", 0)
        
        if date_str:
            try:
                # Parse DD.MM.YY format from database
                date_str = date_str.strip()
                dt = parse_service_date(date_str)
                month_key = dt.strftime("%m%Y")
                month_display = dt.strftime("%m/%Y")
                year = dt.strftime("%Y")
                years_present.add(year)
                
                if month_key not in month_data:
                    month_data[month_key] = {"month": month_display, "invoice_count": 0, "total_amount": 0.0}
                
                month_data[month_key]["invoice_count"] += record_count
                month_data[month_key]["total_amount"] += float(total_amount) if total_amount else 0.0
            except (ValueError, TypeError) as e:
                logger.warning(f"Could not parse date '{date_str}': {e}")
    
    # Fill in all 12 months for each year present
    if years_present:
        for year in sorted(years_present):
            for month_num in range(1, 13):
                month_key = f"{month_num:02d}{year}"
                if month_key not in month_data:
                    month_display = f"{month_num:02d}/{year}"
                    month_data[month_key] = {"month": month_display, "invoice_count": 0, "total_amount": 0.0}
    
    # Convert to sorted list
    return sorted(month_data.values(), key=lambda x: month_sort_key(x["month"]))


@router.get("/analytics/sgbv")
def get_sgbv_data():
    """Get SGB V (event_type SGBV) care record data grouped by month using MongoDB aggregation."""
    try:
        from app.db import CareEventRepository
        
        # Use MongoDB aggregation instead of SQL
        results = CareEventRepository.get_summary_by_event_type("SGBV")
        
        # Format results for API response
        data = _format_date_grouped_analytics(results)
        
        return {
            "status": "ok",
            "data": data
        }
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Operation failed; verify input and database readiness")


@router.get("/analytics/verhinderungspflege")
def get_verhinderungspflege_data():
    """Get VerhinderungsPflege (event_type Verhinderungspflege) care record data grouped by month using MongoDB aggregation."""
    try:
        from app.db import CareEventRepository
        
        # Use MongoDB aggregation instead of SQL
        results = CareEventRepository.get_summary_by_event_type("Verhinderungspflege")
        
        # Format results for API response
        data = _format_date_grouped_analytics(results)
        
        return {
            "status": "ok",
            "data": data
        }
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Operation failed; verify input and database readiness")


@router.get("/analytics/sgbxi")
def get_sgbxi_data():
    """Get SGB XI & Entlastungsleistungen data grouped by month using MongoDB aggregation."""
    try:
        from app.db import CareEventRepository
        
        # Query both SGBXI and Entleistung
        sgbxi_results = CareEventRepository.get_summary_by_event_type("SGBXI")
        entleistung_results = CareEventRepository.get_summary_by_event_type("Entleistung")
        
        # Combine results
        combined_results = sgbxi_results + entleistung_results
        
        # Format results for API response
        data = _format_date_grouped_analytics(combined_results)
        
        return {
            "status": "ok",
            "data": data
        }
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Operation failed; verify input and database readiness")


@router.get("/analytics/sgbv/patient/{patient_id}")
def get_sgbv_by_patient(patient_id: str):
    """Get SGB V care records for a specific patient grouped by month from MongoDB."""
    try:
        from app.db.connection import get_database
        from datetime import datetime
        
        db = get_database()
        org_id = "org_default"
        
        # Try both string and integer formats to handle mixed ID types in DB
        patient_id_int = None
        try:
            patient_id_int = int(patient_id)
        except (ValueError, TypeError):
            pass
        
        # Query with both possible ID formats
        # NOTE: Database stores "SGBV" but we display as "SGB V"
        query = {"org_id": org_id, "event_type": "SGBV"}
        if patient_id_int is not None:
            query["patient_id"] = {"$in": [patient_id, patient_id_int]}
        else:
            query["patient_id"] = patient_id
        
        # Get records grouped by period_start_date
        pipeline = [
            {"$match": query},
            {
                "$group": {
                    "_id": "$period_start_date",
                    "total_amount": {"$sum": {"$toDouble": "$sum_total"}}
                }
            },
            {"$sort": {"_id": 1}}
        ]
        
        rows = list(db.care_events.aggregate(pipeline))
        
        # Build result data, parsing date format and grouping by month
        month_data = {}
        years_present = set()
        
        for row in rows:
            date_str = row.get("_id")
            amount = row.get("total_amount", 0)
            
            if date_str:
                try:
                    # Handle various date formats
                    date_str = str(date_str).strip()
                    # Try DD.MM.YY format first
                    try:
                        dt = parse_service_date(date_str)
                    except ValueError:
                        # Try DD.MM.YYYY format
                        dt = datetime.strptime(date_str, "%d.%m.%Y")
                    
                    month_key = dt.strftime("%m%Y")
                    month_display = dt.strftime("%m/%Y")
                    year = dt.strftime("%Y")
                    years_present.add(year)
                    
                    if month_key not in month_data:
                        month_data[month_key] = {"month": month_display, "total_amount": 0.0}
                    
                    month_data[month_key]["total_amount"] += float(amount) if amount else 0.0
                except (ValueError, TypeError) as pe:
                    logger.warning(f"Could not parse date: {date_str}, error: {pe}")
        
        # Fill in all 12 months for each year present
        if years_present:
            for year in sorted(years_present):
                for month_num in range(1, 13):
                    month_key = f"{month_num:02d}{year}"
                    if month_key not in month_data:
                        month_display = f"{month_num:02d}/{year}"
                        month_data[month_key] = {"month": month_display, "total_amount": 0.0}
        
        # Convert to sorted list
        data = sorted(month_data.values(), key=lambda x: month_sort_key(x["month"]))
        
        return {
            "status": "ok",
            "data": data
        }
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Operation failed; verify input and database readiness")


@router.get("/analytics/verhinderungspflege/patient/{patient_id}")
def get_verhinderungspflege_by_patient(patient_id: str):
    """Get Verhinderungspflege care records for a specific patient grouped by month from MongoDB."""
    try:
        from app.db.connection import get_database
        from datetime import datetime
        
        db = get_database()
        org_id = "org_default"
        
        # Try both string and integer formats to handle mixed ID types in DB
        patient_id_int = None
        try:
            patient_id_int = int(patient_id)
        except (ValueError, TypeError):
            pass
        
        # Query with both possible ID formats
        query = {"org_id": org_id, "event_type": "Verhinderungspflege"}
        if patient_id_int is not None:
            query["patient_id"] = {"$in": [patient_id, patient_id_int]}
        else:
            query["patient_id"] = patient_id
        
        # Get records grouped by period_start_date
        pipeline = [
            {"$match": query},
            {
                "$group": {
                    "_id": "$period_start_date",
                    "total_amount": {"$sum": {"$toDouble": "$sum_total"}}
                }
            },
            {"$sort": {"_id": 1}}
        ]
        
        rows = list(db.care_events.aggregate(pipeline))
        
        # Build result data, parsing date format and grouping by month
        month_data = {}
        years_present = set()
        
        for row in rows:
            date_str = row.get("_id")
            amount = row.get("total_amount", 0)
            
            if date_str:
                try:
                    # Handle various date formats
                    date_str = str(date_str).strip()
                    # Try DD.MM.YY format first
                    try:
                        dt = parse_service_date(date_str)
                    except ValueError:
                        # Try DD.MM.YYYY format
                        dt = datetime.strptime(date_str, "%d.%m.%Y")
                    
                    month_key = dt.strftime("%m%Y")
                    month_display = dt.strftime("%m/%Y")
                    year = dt.strftime("%Y")
                    years_present.add(year)
                    
                    if month_key not in month_data:
                        month_data[month_key] = {"month": month_display, "total_amount": 0.0}
                    
                    month_data[month_key]["total_amount"] += float(amount) if amount else 0.0
                except (ValueError, TypeError) as pe:
                    logger.warning(f"Could not parse date: {date_str}, error: {pe}")
        
        # Fill in all 12 months for each year present
        if years_present:
            for year in sorted(years_present):
                for month_num in range(1, 13):
                    month_key = f"{month_num:02d}{year}"
                    if month_key not in month_data:
                        month_display = f"{month_num:02d}/{year}"
                        month_data[month_key] = {"month": month_display, "total_amount": 0.0}
        
        # Convert to sorted list
        data = sorted(month_data.values(), key=lambda x: month_sort_key(x["month"]))
        
        return {
            "status": "ok",
            "data": data
        }
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Operation failed; verify input and database readiness")


# NEW UNIFIED SCHEMA ANALYTICS ENDPOINTS
# =====================================

@router.get("/analytics/private-invoices")
def get_private_invoices():
    """Get all billable invoices (SGBXI + Entleistung), excluding Consultations."""
    try:
        from app.db.mongodb_config import get_database
        
        db = get_database()
        
        # MongoDB aggregation pipeline
        pipeline = [
            {
                "$match": {
                    "org_id": "org_default",
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
            {"$unwind": {"path": "$patient", "preserveNullAndEmptyArrays": True}},
            {
                "$lookup": {
                    "from": "billing_details",
                    "let": {"care_event_id": "$care_event_id"},
                    "pipeline": [
                        {"$match": {"$expr": {"$eq": ["$care_event_id", "$$care_event_id"]}}}
                    ],
                    "as": "billing"
                }
            },
            {"$unwind": {"path": "$billing", "preserveNullAndEmptyArrays": True}},
            {"$sort": {"created_at": -1}}
        ]
        
        results = list(db.care_events.aggregate(pipeline))
        
        invoices = []
        for doc in results:
            billing = doc.get("billing") or {}
            patient = doc.get("patient") or {}
            invoices.append({
                "care_event_id": doc.get("care_event_id"),
                "patient_name": patient.get("patient_name", ""),
                "period_start_date": doc.get("period_start_date"),
                "period_end_date": doc.get("period_end_date"),
                "billing_status": billing.get("billing_status", "pending"),
                "amount_owed": float(billing.get("amount_owed", 0) or 0),
                "sum_total": float(doc.get("sum_total", 0) or 0),
            })
        
        return {
            "status": "ok",
            "invoices": invoices
        }
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Operation failed; verify input and database readiness")


@router.get("/analytics/consultations")
def get_consultations_data():
    """Get Consultations (event_type Consultation) grouped by month using MongoDB aggregation."""
    try:
        from app.db import CareEventRepository
        
        # Use MongoDB aggregation instead of SQL
        results = CareEventRepository.get_summary_by_event_type("Consultation")
        
        # Format results for API response
        data = _format_date_grouped_analytics(results)
        
        return {
            "status": "ok",
            "data": data
        }
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Operation failed; verify input and database readiness")


@router.get("/analytics/ausbildungspauschale")
def get_ausbildungspauschale_data():
    """Get Ausbildungspauschale (service_code 01013021) grouped by month."""
    try:
        from app.db.connection import get_database

        db = get_database()
        org_id = "org_default"

        pipeline = [
            {"$match": {"org_id": org_id}},
            {"$unwind": "$services"},
            {"$match": {"services.service_code": "01013021"}},
            {
                "$group": {
                    "_id": "$period_start_date",
                    "record_count": {"$sum": 1},
                    "total_amount": {"$sum": {"$toDouble": "$services.line_total"}}
                }
            },
            {"$sort": {"_id": 1}}
        ]

        rows = list(db.care_events.aggregate(pipeline))
        data = _format_date_grouped_analytics(rows)

        return {
            "status": "ok",
            "data": data
        }
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Operation failed; verify input and database readiness")


@router.get("/analytics/sgbxi-leistungscodes")
def get_sgbxi_leistungscodes():
    """Get abgerechnete SGB XI Leistungen (event_type SGBXI) grouped by Leistungscode, per month and year."""
    try:
        from app.db.connection import get_database

        db = get_database()
        org_id = "org_default"

        pipeline = [
            {"$match": {"org_id": org_id, "event_type": "SGBXI"}},
            {"$unwind": "$services"},
            # Erstbesuch is outside the operational service-code comparison.
            {"$match": {"services.service_code": {"$ne": "01010018"}}},
            {
                "$group": {
                    "_id": {
                        "invoicing_month": "$invoicing_month",
                        "code": "$services.service_code",
                    },
                    "description": {"$first": "$services.service_description"},
                    "quantity": {
                        "$sum": {
                            "$convert": {
                                "input": "$services.quantity_value",
                                "to": "double",
                                "onError": 0,
                                "onNull": 0,
                            }
                        }
                    },
                }
            },
        ]

        rows = list(db.care_events.aggregate(pipeline))

        years: dict = {}

        for row in rows:
            invoicing_month = row["_id"].get("invoicing_month")
            code = row["_id"].get("code")
            description = row.get("description") or ""
            quantity = row.get("quantity", 0)
            # invoicing_month is the authoritative billing-month field set at
            # import time (MMYYYY); it is not always the same calendar month as
            # a parsed period_start_date (period can start in a prior month),
            # so grouping here must use it rather than parsing service dates.
            if not invoicing_month or not code or len(str(invoicing_month)) != 6:
                continue
            month, year = str(invoicing_month)[:2], str(invoicing_month)[2:]

            year_bucket = years.setdefault(year, {"months": {}, "total": {}})
            month_bucket = year_bucket["months"].setdefault(month, {})

            entry = month_bucket.setdefault(
                code, {"service_code": code, "service_description": description, "quantity": 0.0}
            )
            entry["quantity"] += quantity

            total_entry = year_bucket["total"].setdefault(
                code, {"service_code": code, "service_description": description, "quantity": 0.0}
            )
            total_entry["quantity"] += quantity

        data = {}
        for year, buckets in years.items():
            months_out = {}
            for month_num in range(1, 13):
                month_key = f"{month_num:02d}"
                month_rows = list(buckets["months"].get(month_key, {}).values())
                for r in month_rows:
                    r["quantity"] = round(r["quantity"], 2)
                month_rows.sort(key=lambda r: str(r["service_code"]))
                months_out[month_key] = {
                    "label": GERMAN_MONTHS[month_key],
                    "rows": month_rows,
                }
            total_rows = list(buckets["total"].values())
            for r in total_rows:
                r["quantity"] = round(r["quantity"], 2)
            total_rows.sort(key=lambda r: str(r["service_code"]))
            data[year] = {"months": months_out, "total": total_rows}

        return {
            "status": "ok",
            "data": data
        }
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Operation failed; verify input and database readiness")


@router.get("/analytics/sgbxi-leistungscodes/audit")
def get_sgbxi_leistungscodes_audit(year: int):
    """Diff the stored-data (care_events) SGB XI Leistungspunkte totals against the
    independent PDF audit table (pdf_audit_lines), per month and Leistungscode.

    Scoped to the '0101...' Leistungspunkte code family, matching the PDF audit
    extractor's scope. Does not read or write care_events/billing collections;
    the comparison is computed on read from both sides."""
    try:
        from app.db.connection import get_database

        db = get_database()
        org_id = "org_default"

        # Stored-data side: same aggregation as /analytics/sgbxi-leistungscodes,
        # restricted to the 0101 family, with euro total added.
        care_pipeline = [
            {"$match": {"org_id": org_id, "event_type": "SGBXI"}},
            {"$unwind": "$services"},
            {"$match": {"services.service_code": {"$regex": "^0101", "$ne": "01010018"}}},
            {
                "$group": {
                    "_id": {"invoicing_month": "$invoicing_month", "code": "$services.service_code"},
                    "description": {"$first": "$services.service_description"},
                    "quantity": {
                        "$sum": {"$convert": {"input": "$services.quantity_value", "to": "double", "onError": 0, "onNull": 0}}
                    },
                    "line_total": {
                        "$sum": {"$convert": {"input": "$services.line_total", "to": "double", "onError": 0, "onNull": 0}}
                    },
                }
            },
        ]
        care_rows = list(db.care_events.aggregate(care_pipeline))

        dashboard = {}
        for row in care_rows:
            invoicing_month = row["_id"].get("invoicing_month")
            code = row["_id"].get("code")
            if not invoicing_month or not code or len(str(invoicing_month)) != 6:
                continue
            month, row_year = str(invoicing_month)[:2], int(str(invoicing_month)[2:])
            if row_year != year:
                continue
            key = (month, code)
            entry = dashboard.setdefault(key, {"description": row.get("description") or "", "quantity": 0.0, "line_total": 0.0})
            entry["quantity"] += row.get("quantity", 0)
            entry["line_total"] += row.get("line_total", 0)

        # PDF-audit side: raw extracted lines, same scope.
        audit_pipeline = [
            {"$match": {"org_id": org_id, "year": year, "service_code": {"$regex": "^0101", "$ne": "01010018"}}},
            {
                "$group": {
                    "_id": {"month": "$billing_month", "code": "$service_code"},
                    "description": {"$first": "$service_description"},
                    "quantity": {"$sum": "$quantity"},
                    "line_total": {"$sum": "$line_total"},
                }
            },
        ]
        audit_rows = list(db.pdf_audit_lines.aggregate(audit_pipeline))

        audit = {}
        for row in audit_rows:
            billing_month = row["_id"].get("month")
            code = row["_id"].get("code")
            if not billing_month or not code or len(billing_month) != 6:
                continue
            month, row_year = billing_month[:2], int(billing_month[2:])
            if row_year != year:
                continue
            key = (month, code)
            entry = audit.setdefault(key, {"description": row.get("description") or "", "quantity": 0.0, "line_total": 0.0})
            entry["quantity"] += row.get("quantity", 0)
            entry["line_total"] += row.get("line_total", 0)

        documents = list(db.pdf_audit_documents.find({"org_id": org_id, "year": year}, {"_id": 0}))

        months_out = {}
        for month_num in range(1, 13):
            month_key = f"{month_num:02d}"
            codes = set(k[1] for k in dashboard if k[0] == month_key) | set(k[1] for k in audit if k[0] == month_key)
            rows = []
            for code in codes:
                d = dashboard.get((month_key, code), {"description": "", "quantity": 0.0, "line_total": 0.0})
                a = audit.get((month_key, code), {"description": "", "quantity": 0.0, "line_total": 0.0})
                rows.append({
                    "service_code": code,
                    "service_description": a["description"] or d["description"],
                    "dashboard_quantity": round(d["quantity"], 2),
                    "audit_quantity": round(a["quantity"], 2),
                    "quantity_diff": round(a["quantity"] - d["quantity"], 2),
                    "dashboard_total_eur": round(d["line_total"], 2),
                    "audit_total_eur": round(a["line_total"], 2),
                    "eur_diff": round(a["line_total"] - d["line_total"], 2),
                })
            rows.sort(key=lambda r: str(r["service_code"]))
            months_out[month_key] = {"label": GERMAN_MONTHS[month_key], "rows": rows}

        return {
            "status": "ok",
            "year": year,
            "documents": documents,
            "months": months_out,
        }
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Operation failed; verify input and database readiness")


@router.get("/analytics/consultations/patient/{patient_id}")
def get_consultations_by_patient(patient_id: str):
    """Get Consultations for a specific patient grouped by month from MongoDB."""
    try:
        from app.db.connection import get_database
        from datetime import datetime
        
        db = get_database()
        org_id = "org_default"
        
        # Try both string and integer formats to handle mixed ID types in DB
        patient_id_int = None
        try:
            patient_id_int = int(patient_id)
        except (ValueError, TypeError):
            pass
        
        # Query with both possible ID formats
        query = {"org_id": org_id, "event_type": "Consultation"}
        if patient_id_int is not None:
            query["patient_id"] = {"$in": [patient_id, patient_id_int]}
        else:
            query["patient_id"] = patient_id
        
        # Get records grouped by period_start_date
        pipeline = [
            {"$match": query},
            {
                "$group": {
                    "_id": "$period_start_date",
                    "total_amount": {"$sum": {"$toDouble": "$sum_total"}}
                }
            },
            {"$sort": {"_id": 1}}
        ]
        
        rows = list(db.care_events.aggregate(pipeline))
        
        month_data = {}
        years_present = set()
        
        for row in rows:
            date_str = row.get("_id")
            amount = row.get("total_amount", 0)
            
            if date_str:
                try:
                    # Handle various date formats
                    date_str = str(date_str).strip()
                    # Try DD.MM.YY format first
                    try:
                        dt = parse_service_date(date_str)
                    except ValueError:
                        # Try DD.MM.YYYY format
                        dt = datetime.strptime(date_str, "%d.%m.%Y")
                    
                    month_key = dt.strftime("%m%Y")
                    month_display = dt.strftime("%m/%Y")
                    year = dt.strftime("%Y")
                    years_present.add(year)
                    
                    if month_key not in month_data:
                        month_data[month_key] = {"month": month_display, "total_amount": 0.0}
                    
                    month_data[month_key]["total_amount"] += float(amount) if amount else 0.0
                except (ValueError, TypeError) as pe:
                    logger.warning(f"Could not parse date: {date_str}, error: {pe}")
        
        # Fill in all 12 months for each year present
        if years_present:
            for year in sorted(years_present):
                for month_num in range(1, 13):
                    month_key = f"{month_num:02d}{year}"
                    if month_key not in month_data:
                        month_display = f"{month_num:02d}/{year}"
                        month_data[month_key] = {"month": month_display, "total_amount": 0.0}
        
        data = sorted(month_data.values(), key=lambda x: month_sort_key(x["month"]))
        
        return {
            "status": "ok",
            "data": data
        }
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Operation failed; verify input and database readiness")


@router.get("/analytics/ausbildungspauschale/patient/{patient_id}")
def get_ausbildungspauschale_by_patient(patient_id: str):
    """Get Ausbildungspauschale (service_code 01013021) for a patient grouped by month."""
    try:
        from app.db.connection import get_database
        from datetime import datetime

        db = get_database()
        org_id = "org_default"

        patient_id_int = None
        try:
            patient_id_int = int(patient_id)
        except (ValueError, TypeError):
            pass

        match_query = {"org_id": org_id, "services.service_code": "01013021"}
        if patient_id_int is not None:
            match_query["patient_id"] = {"$in": [patient_id, patient_id_int]}
        else:
            match_query["patient_id"] = patient_id

        pipeline = [
            {"$match": match_query},
            {"$unwind": "$services"},
            {"$match": {"services.service_code": "01013021"}},
            {
                "$group": {
                    "_id": "$period_start_date",
                    "total_amount": {"$sum": {"$toDouble": "$services.line_total"}}
                }
            },
            {"$sort": {"_id": 1}}
        ]

        rows = list(db.care_events.aggregate(pipeline))

        month_data = {}
        years_present = set()

        for row in rows:
            date_str = row.get("_id")
            amount = row.get("total_amount", 0)

            if date_str:
                try:
                    date_str = str(date_str).strip()
                    try:
                        dt = parse_service_date(date_str)
                    except ValueError:
                        dt = datetime.strptime(date_str, "%d.%m.%Y")

                    month_key = dt.strftime("%m%Y")
                    month_display = dt.strftime("%m/%Y")
                    year = dt.strftime("%Y")
                    years_present.add(year)

                    if month_key not in month_data:
                        month_data[month_key] = {"month": month_display, "total_amount": 0.0}

                    month_data[month_key]["total_amount"] += float(amount) if amount else 0.0
                except (ValueError, TypeError) as pe:
                    logger.warning(f"Could not parse date: {date_str}, error: {pe}")

        if years_present:
            for year in sorted(years_present):
                for month_num in range(1, 13):
                    month_key = f"{month_num:02d}{year}"
                    if month_key not in month_data:
                        month_display = f"{month_num:02d}/{year}"
                        month_data[month_key] = {"month": month_display, "total_amount": 0.0}

        data = sorted(month_data.values(), key=lambda x: month_sort_key(x["month"]))

        return {
            "status": "ok",
            "data": data
        }
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Operation failed; verify input and database readiness")
