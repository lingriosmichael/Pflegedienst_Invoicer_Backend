import traceback
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import app.database as database
import app.invoice_generator as invoice_generator
import app.pdf_parser as pdf_parser
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()

class InvoiceRequest(BaseModel):
    abrechnungsmonat: str
@router.post("/generate_invoices")
def generate_invoices(req: InvoiceRequest):
    # First mark invoices ready based on amount_owed and service packet flag
    database.mark_month_ready_for_generation(req.abrechnungsmonat, legacy_entleistung=True)
    # Then generate the invoices
    result = invoice_generator.process_generate_invoices(req.abrechnungsmonat)
    return {
        "status": "ok",
        "message": "Rechnungen erstellt",
        "abrechnungsmonat": req.abrechnungsmonat,
        "generated": result["generated"],
        "total_cases": result["total_cases"],
        "failed": result["failed"],
    }


@router.post("/regenerate_invoices")
def regenerate_invoices(req: InvoiceRequest):
    """
    Regenerate PDFs for an already prepared month without re-running mark_ready.

    This reuses the existing invoice numbers where present and only includes
    billing rows whose linked care_event period actually belongs to the
    requested month.
    """
    result = invoice_generator.process_generate_invoices(
        req.abrechnungsmonat,
        include_orphaned=False,
        require_invoice_needed=False,
    )
    return {
        "status": "ok",
        "message": "Rechnungen neu erstellt",
        "abrechnungsmonat": req.abrechnungsmonat,
        "generated": result["generated"],
        "total_cases": result["total_cases"],
        "failed": result["failed"],
    }


@router.post("/complete_data")
def complete_data(req: InvoiceRequest):
    database.check_missing_patient_fields(req.abrechnungsmonat, auto_fix=True)
    database.check_service_fields(auto_fix=True)
    return {"status": "ok", "message": "Patientendaten ergänzt und Leistungsdaten überprüft"}


@router.post("/check_service_fields")
def check_service_fields():
    database.check_service_fields(auto_fix=False)
    return {"status": "ok", "message": "Leistungsdaten überprüft"}


@router.post("/retry_failed")
def retry_failed():
    pdf_parser.refeed_failed_chunk_from_file()
    return {"status": "ok", "message": "Fehlerhafte Abrechnungen erneut verarbeitet"}
class MarkReadyRequest(BaseModel):
    invoicing_month: str   # "MMYYYY"
    only_positive: bool = False

@router.post("/mark_ready")
def mark_ready(req: MarkReadyRequest):
    try:
        m = (req.invoicing_month or "").strip()
        if len(m) != 6 or not m.isdigit():
            raise HTTPException(status_code=400, detail="invoicing_month must be MMYYYY")

        updated = database.mark_month_ready_for_generation(m, req.only_positive, legacy_entleistung=False)
        return {"status": "ok", "updated": updated}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/mark_ready error:\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/mark_ready_legacy")
def mark_ready_legacy(req: MarkReadyRequest):
    """
    Mark month ready for generation using LEGACY Entleistung logic.
    Legacy logic: Anything > 125 EUR per month gets invoiced (simple monthly threshold).
    Use this for December 2025 and earlier months before the yearly cumulative logic was implemented.
    """
    try:
        m = (req.invoicing_month or "").strip()
        if len(m) != 6 or not m.isdigit():
            raise HTTPException(status_code=400, detail="invoicing_month must be MMYYYY")

        updated = database.mark_month_ready_for_generation(m, req.only_positive, legacy_entleistung=True)
        return {"status": "ok", "updated": updated}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/mark_ready_legacy error:\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/validate_sgbxi_amounts")
async def validate_sgbxi_amounts():
    """
    Validate all SGBXI records and auto-correct any reversed sum_covered/sum_total.
    
    Rule: sum_covered must always be <= sum_total
    If sum_covered > sum_total, they are automatically swapped.
    
    Returns: counts of corrections made and total records checked.
    """
    try:
        result = database.validate_and_fix_sgbxi_amounts()
        return {
            "status": "success",
            "corrected_count": result['corrected_count'],
            "total_checked": result['total_checked'],
            "message": f"Corrected {result['corrected_count']} out of {result['total_checked']} SGBXI records"
        }
    except Exception as e:
        logger.error(f"/validate_sgbxi_amounts error:\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))
