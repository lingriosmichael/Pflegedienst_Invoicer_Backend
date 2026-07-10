import traceback
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import app.database as database
from app.core.logging import get_logger
from app.entlastung_balance import uses_new_entlastung_logic, recompute_entlastung_for_care_event

logger = get_logger(__name__)

router = APIRouter()

@router.get("/billing/patients")
def get_billing_patients():
    """Get list of patients with billing details."""
    try:
        from app.db.connection import get_database
        db = get_database()
        org_id = "org_default"
        
        # Get all patient_ids with any billing details
        all_care_event_ids = db.billing_details.distinct(
            "care_event_id", 
            {"org_id": org_id}
        )
        
        # Get patient_ids from those care_events
        patient_ids = db.care_events.distinct(
            "patient_id",
            {"org_id": org_id, "care_event_id": {"$in": all_care_event_ids}}
        )
        
        # Get patient info with billing counts
        patients = []
        for pid in patient_ids:
            patient = db.patient_profiles.find_one({"patient_id": pid})
            if patient:
                # Count all billing details for this patient
                patient_care_events = db.care_events.distinct(
                    "care_event_id",
                    {"org_id": org_id, "patient_id": pid}
                )
                billing_count = db.billing_details.count_documents({
                    "org_id": org_id,
                    "care_event_id": {"$in": patient_care_events}
                })
                patients.append({
                    "id": pid,
                    "name": patient.get("patient_name", "Unknown"),
                    "pending_count": billing_count
                })
        
        # Sort by name
        patients.sort(key=lambda x: x["name"])
        
        return {"status": "ok", "patients": patients}
    except Exception as e:
        logger.error(f"Billing patients error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/billing/patient/{patient_id}/pending")
def get_patient_pending_billing(patient_id: str):
    """Get all billing details for a patient."""
    try:
        from app.db.connection import get_database
        db = get_database()
        org_id = "org_default"
        
        # Get care_events for this patient
        care_events = list(db.care_events.find(
            {"org_id": org_id, "patient_id": patient_id}
        ))
        care_event_map = {ce["care_event_id"]: ce for ce in care_events}
        care_event_ids = list(care_event_map.keys())
        
        # Get ALL billing_details for these care_events (no status filter)
        billing_details = list(db.billing_details.find({
            "org_id": org_id,
            "care_event_id": {"$in": care_event_ids}
        }))
        
        # Enrich with care_event info
        result = []
        for bd in billing_details:
            ce = care_event_map.get(bd["care_event_id"], {})
            result.append({
                "billing_detail_id": bd.get("billing_detail_id"),
                "care_event_id": bd.get("care_event_id"),
                "invoicing_month": bd.get("invoicing_month"),
                "sum_covered": bd.get("sum_covered", 0),
                "sum_total": bd.get("sum_total", 0),
                "amount_owed": bd.get("amount_owed", 0),
                "billing_status": bd.get("billing_status"),
                "invoice_number": bd.get("invoice_number"),
                "event_type": ce.get("event_type", ""),
                "period_start_date": ce.get("period_start_date", ""),
                "period_end_date": ce.get("period_end_date", ""),
                "care_account": ce.get("care_account", ""),
                "services_count": len(ce.get("services", []))
            })
        
        # Sort by period_start_date (latest first)
        def parse_german_date(date_str):
            """Parse DD.MM.YY to sortable tuple (year, month, day)"""
            if not date_str:
                return (0, 0, 0)
            try:
                parts = date_str.split(".")
                if len(parts) == 3:
                    day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
                    # Convert 2-digit year to 4-digit
                    year = year + 2000 if year < 50 else year + 1900
                    return (year, month, day)
            except:
                pass
            return (0, 0, 0)
        
        result.sort(key=lambda x: parse_german_date(x.get("period_start_date", "")), reverse=True)
        
        return {"status": "ok", "billing_details": result}
    except Exception as e:
        logger.error(f"Patient pending billing error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/billing/detail/{billing_detail_id}/services")
def get_billing_detail_services(billing_detail_id: str):
    """Get services for a specific billing detail."""
    try:
        from app.db.connection import get_database
        db = get_database()
        org_id = "org_default"
        
        # Get billing_detail
        bd = db.billing_details.find_one({
            "org_id": org_id,
            "billing_detail_id": billing_detail_id
        })
        if not bd:
            raise HTTPException(status_code=404, detail="Billing detail not found")
        
        # Get linked care_event
        ce = db.care_events.find_one({
            "org_id": org_id,
            "care_event_id": bd["care_event_id"]
        })
        if not ce:
            raise HTTPException(status_code=404, detail="Care event not found")
        
        services = ce.get("services", [])
        
        return {
            "status": "ok",
            "billing_detail_id": billing_detail_id,
            "care_event_id": bd["care_event_id"],
            "services": services,
            "sum_covered": bd.get("sum_covered", 0),
            "sum_total": bd.get("sum_total", 0)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get services error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


class ServiceUpdate(BaseModel):
    service_code: str
    service_description: str
    quantity_value: float
    unit_price: float
    line_total: Optional[float] = None


class ServicesUpdateRequest(BaseModel):
    services: List[ServiceUpdate]
    sum_covered: Optional[float] = None


@router.put("/billing/detail/{billing_detail_id}/services")
def update_billing_detail_services(billing_detail_id: str, request: ServicesUpdateRequest):
    """Update services for a billing detail (edit/delete services)."""
    try:
        from app.db.connection import get_database
        db = get_database()
        org_id = "org_default"
        
        # Get billing_detail
        bd = db.billing_details.find_one({
            "org_id": org_id,
            "billing_detail_id": billing_detail_id
        })
        if not bd:
            raise HTTPException(status_code=404, detail="Billing detail not found")

        ce = db.care_events.find_one({
            "org_id": org_id,
            "care_event_id": bd["care_event_id"]
        })
        if not ce:
            raise HTTPException(status_code=404, detail="Care event not found")
        
        # Build updated services with recalculated line_totals
        updated_services = []
        new_sum_total = 0
        for svc in request.services:
            line_total = svc.quantity_value * svc.unit_price
            updated_services.append({
                "service_code": svc.service_code,
                "service_description": svc.service_description,
                "quantity_value": svc.quantity_value,
                "unit_price": svc.unit_price,
                "line_total": round(line_total, 2),
                "updated_at": datetime.now().isoformat()
            })
            new_sum_total += line_total
        
        new_sum_total = round(new_sum_total, 2)
        event_type = ce.get("event_type", "")
        invoicing_month = bd.get("invoicing_month", "")

        entlastung_recompute = None
        if event_type == "Entleistung" and uses_new_entlastung_logic(invoicing_month):
            # Balance-tracked months: covered/owed always come from the balance,
            # never from a manually typed value, so used_amount stays in sync.
            previously_covered = bd.get("sum_covered", 0) or 0
            entlastung_recompute = recompute_entlastung_for_care_event(
                ce.get("patient_id"), invoicing_month, new_sum_total, previously_covered
            )
            new_sum_covered = entlastung_recompute["covered"]
        elif request.sum_covered is not None:
            new_sum_covered = request.sum_covered
        elif event_type == "Entleistung":
            new_sum_covered = min(new_sum_total, 127.35)
        else:
            new_sum_covered = new_sum_total

        investitionskosten = round(new_sum_total * 0.06, 2) if event_type == "SGBXI" else 0.0
        if entlastung_recompute is not None:
            new_amount_owed = entlastung_recompute["owed"]
        elif event_type == "SGBXI":
            new_amount_owed = round(max(new_sum_total - new_sum_covered + investitionskosten, 0), 2)
        else:
            new_amount_owed = round(max(new_sum_total - new_sum_covered, 0), 2)

        # Update care_event services and totals
        db.care_events.update_one(
            {"org_id": org_id, "care_event_id": bd["care_event_id"]},
            {
                "$set": {
                    "services": updated_services,
                    "sum_total": new_sum_total,
                    "sum_covered": new_sum_covered,
                    "updated_at": datetime.now().isoformat()
                }
            }
        )

        # Update billing_detail totals
        db.billing_details.update_one(
            {"org_id": org_id, "billing_detail_id": billing_detail_id},
            {
                "$set": {
                    "sum_total": new_sum_total,
                    "sum_covered": new_sum_covered,
                    "investitionskosten": investitionskosten,
                    "amount_owed": new_amount_owed,
                    "updated_at": datetime.now().isoformat()
                }
            }
        )
        
        return {
            "status": "ok",
            "message": "Services updated successfully",
            "sum_total": new_sum_total,
            "sum_covered": new_sum_covered,
            "amount_owed": new_amount_owed,
            "services_count": len(updated_services)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Update services error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/billing/detail/{billing_detail_id}/regenerate-pdf")
def regenerate_billing_pdf(billing_detail_id: str):
    """Regenerate invoice PDF for a billing detail."""
    try:
        from app.db.connection import get_database
        from app.db.mongodb_repositories import InvoiceRepository
        import subprocess
        
        db = get_database()
        org_id = "org_default"
        
        # Get billing_detail
        bd = db.billing_details.find_one({
            "org_id": org_id,
            "billing_detail_id": billing_detail_id
        })
        if not bd:
            raise HTTPException(status_code=404, detail="Billing detail not found")
        
        # Get linked care_event
        ce = db.care_events.find_one({
            "org_id": org_id,
            "care_event_id": bd["care_event_id"]
        })
        if not ce:
            raise HTTPException(status_code=404, detail="Care event not found")
        
        # Get patient info
        patient_doc = db.patient_profiles.find_one({
            "patient_id": ce["patient_id"]
        })
        if not patient_doc:
            raise HTTPException(status_code=404, detail="Patient not found")
        
        event_type = ce.get("event_type", "")
        amounts = database._resolve_invoice_amounts(event_type, ce, bd)
        
        # Assign invoice number if not present
        invoice_number = bd.get("invoice_number")
        if not invoice_number:
            invoice_number = InvoiceRepository.get_next_invoice_number()
            # Update billing_detail with invoice number
            db.billing_details.update_one(
                {"org_id": org_id, "billing_detail_id": billing_detail_id},
                {"$set": {"invoice_number": invoice_number, "updated_at": datetime.now().isoformat()}}
            )
        
        # Build address from split fields
        street_name = patient_doc.get("street_name", "") or ""
        street_number = patient_doc.get("street_number", "") or ""
        postal_code = patient_doc.get("postal_code", "") or ""
        city = patient_doc.get("city", "") or ""
        address = f"{street_name} {street_number} {postal_code} {city}".strip()
        
        # Map services from MongoDB format to template format
        mongo_services = ce.get("services", [])
        mapped_services = [
            {
                "id": idx,
                "code": svc.get("service_code", ""),
                "description": svc.get("service_description", ""),
                "quantity": svc.get("quantity_value", 0),
                "unit_price": svc.get("unit_price", 0),
                "total_price": svc.get("line_total", 0)
            }
            for idx, svc in enumerate(mongo_services)
        ]
        
        # Build data dict matching what generate_invoice_pdf expects
        data = {
            "patient": {
                "id": patient_doc.get("patient_id"),
                "name": patient_doc.get("patient_name", ""),
                "birthdate": patient_doc.get("date_of_birth", ""),
                "insurance_number": patient_doc.get("insurance_number", ""),
                "care_level": patient_doc.get("care_level", ""),
                "include_service_packet": patient_doc.get("include_service_packet", 0) if event_type == "SGBXI" else 0,
                "address": address,
                "debtor_number": patient_doc.get("debtor_id", "") or patient_doc.get("debtor_number", ""),
            },
            "invoice": {
                "id": ce.get("care_event_id"),
                "invoicing_month": bd.get("invoicing_month", ""),
                "invoice_number": invoice_number,
                "care_account": ce.get("care_account", ""),
                "event_type": event_type,
                "care_range_begin": ce.get("period_start_date", ""),
                "care_range_end": ce.get("period_end_date", ""),
                "sum_total": amounts["sum_total"],
                "sum_covered": amounts["sum_covered"],
                "amount_owed": amounts["amount_owed"],
            },
            "services": mapped_services
        }
        
        # Import invoice generator
        from app.invoice_generator import generate_invoice_pdf
        
        # Generate PDF
        pdf_path = generate_invoice_pdf(data)
        
        return {
            "status": "ok",
            "message": "PDF generated successfully",
            "pdf_path": str(pdf_path)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Regenerate PDF error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/billing/detail/{billing_detail_id}/view-pdf")
def view_billing_pdf(billing_detail_id: str):
    """Open the generated PDF for a billing detail."""
    try:
        from app.db.connection import get_database
        import subprocess
        from pathlib import Path
        
        db = get_database()
        org_id = "org_default"
        
        # Get billing_detail
        bd = db.billing_details.find_one({
            "org_id": org_id,
            "billing_detail_id": billing_detail_id
        })
        if not bd:
            raise HTTPException(status_code=404, detail="Billing detail not found")
        
        invoice_number = bd.get("invoice_number")
        if not invoice_number:
            raise HTTPException(status_code=400, detail="Keine Rechnung generiert. Bitte zuerst PDF generieren.")
        
        # Find PDF file by invoice number
        output_dir = Path("output/invoices")
        pdf_files = list(output_dir.glob(f"*{invoice_number}*.pdf"))
        
        if not pdf_files:
            raise HTTPException(status_code=404, detail=f"PDF nicht gefunden für Rechnungsnummer {invoice_number}")
        
        # Open the most recent matching PDF
        pdf_path = max(pdf_files, key=lambda p: p.stat().st_mtime)
        subprocess.run(["open", str(pdf_path)], check=False)
        
        return {
            "status": "ok",
            "message": "PDF opened",
            "pdf_path": str(pdf_path)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"View PDF error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


class BillingStatusUpdate(BaseModel):
    billing_status: str


@router.patch("/billing/detail/{billing_detail_id}/status")
def update_billing_status(billing_detail_id: str, request: BillingStatusUpdate):
    """Update the billing status of a billing detail."""
    valid_statuses = ["", "invoice_needed", "sent", "paid"]
    if request.billing_status not in valid_statuses:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid status. Must be one of: {valid_statuses}"
        )
    
    try:
        from app.db.connection import get_database
        db = get_database()
        
        org_id = "org_default"
        result = db.billing_details.update_one(
            {"org_id": org_id, "billing_detail_id": billing_detail_id},
            {"$set": {"billing_status": request.billing_status}}
        )
        
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Billing detail not found")
        
        return {"status": "ok", "billing_status": request.billing_status}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Update billing status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
