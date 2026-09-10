import traceback
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db.transactions import transactional
from app.utils.parsing import generate_id
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter()

class PatientRequest(BaseModel):
    name: str
    birthdate: str
    insurance_number: str
    care_level: str
    address: str = ""
    street_name: str = ""
    street_number: str = ""
    postal_code: str = ""
    city: str = ""
    debtor_number: str = ""
    include_service_packet: bool = False

@router.get("/patients")
def list_patients():
    """List all patients from MongoDB."""
    try:
        from app.db.mongodb_repositories import PatientRepository
        
        patients = PatientRepository.find_all("org_default")
        return {
            "status": "ok",
            "patients": [
                {
                    "id": p.get("patient_id"),
                    "name": p.get("patient_name", ""),
                    "birthdate": p.get("date_of_birth", ""),
                    "insurance_number": p.get("insurance_number", ""),
                    "care_level": p.get("care_level", ""),
                    "address": f"{p.get('street_name', '')} {p.get('street_number', '')}, {p.get('postal_code', '')} {p.get('city', '')}" if p.get("street_name") else "",
                    "street_name": p.get("street_name", ""),
                    "street_number": p.get("street_number", ""),
                    "postal_code": p.get("postal_code", ""),
                    "city": p.get("city", ""),
                    "debtor_number": p.get("debtor_id", "") or "",
                    "include_service_packet": bool(p.get("include_service_packet", False)),
                    "needs_review": bool(p.get("needs_review", False)),
                    "review_reasons": p.get("review_reasons", []),
                }
                for p in patients
            ]
        }
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Operation failed; verify input and database readiness")

@router.post("/patient")
def create_patient(req: PatientRequest):
    """Create a new patient in MongoDB."""
    try:
        from app.db.mongodb_repositories import PatientRepository
        from app.db.connection import get_database
        from datetime import datetime
        import time
        
        org_id = "org_default"
        next_patient_id = generate_id("pat")

        patient_data = {
            "patient_id": next_patient_id,
            "patient_name": req.name,
            "date_of_birth": req.birthdate,
            "insurance_number": req.insurance_number,
            "care_level": req.care_level,
            "street_name": getattr(req, 'street_name', ''),
            "street_number": getattr(req, 'street_number', ''),
            "postal_code": getattr(req, 'postal_code', ''),
            "city": getattr(req, 'city', ''),
            "include_service_packet": bool(req.include_service_packet),
            "debtor_id": req.debtor_number
        }
        
        PatientRepository.create(patient_data, org_id)
        return {"status": "ok", "patient_id": str(next_patient_id)}
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Operation failed; verify input and database readiness")

@router.get("/patient/{patient_id}")
def get_patient(patient_id: str):
    """Get a single patient by ID from MongoDB."""
    try:
        from app.db.mongodb_repositories import PatientRepository
        
        patient = PatientRepository.find_by_id(patient_id, "org_default")
        return {
            "status": "ok",
            "patient": {
                "id": patient.get("patient_id"),
                "name": patient.get("patient_name", ""),
                "birthdate": patient.get("date_of_birth", ""),
                "insurance_number": patient.get("insurance_number", ""),
                "care_level": patient.get("care_level", ""),
                "street_name": patient.get("street_name", ""),
                "street_number": patient.get("street_number", ""),
                "postal_code": patient.get("postal_code", ""),
                "city": patient.get("city", ""),
                "address": f"{patient.get('street_name', '')} {patient.get('street_number', '')}, {patient.get('postal_code', '')} {patient.get('city', '')}" if patient.get("street_name") else "",
                "debtor_number": patient.get("debtor_id", ""),
                "include_service_packet": bool(patient.get("include_service_packet", False)),
                "needs_review": bool(patient.get("needs_review", False)),
                "review_reasons": patient.get("review_reasons", []),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Operation failed; verify input and database readiness")

@router.put("/patient/{patient_id}")
@transactional
def update_patient(patient_id: str, req: PatientRequest):
    """Update an existing patient in MongoDB."""
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
        query = {"org_id": org_id}
        if patient_id_int is not None:
            query["patient_id"] = {"$in": [patient_id, patient_id_int]}
        else:
            query["patient_id"] = patient_id
        
        update_data = {
            "patient_name": req.name,
            "date_of_birth": req.birthdate,
            "insurance_number": req.insurance_number,
            "care_level": req.care_level,
            "street_name": getattr(req, 'street_name', ''),
            "street_number": getattr(req, 'street_number', ''),
            "postal_code": getattr(req, 'postal_code', ''),
            "city": getattr(req, 'city', ''),
            "include_service_packet": bool(req.include_service_packet),
            "debtor_id": getattr(req, 'debtor_number', ''),
            "updated_at": datetime.utcnow()
        }
        # A manual edit that supplies a birthdate resolves the import-time
        # "missing_or_invalid_birthdate" flag set by app.import_records.
        if req.birthdate.strip():
            update_data["needs_review"] = False
            update_data["review_reasons"] = []

        result = db.patient_profiles.update_one(
            query,
            {"$set": update_data}
        )
        
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Patient not found")
        
        return {"status": "ok", "updated": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Operation failed (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Operation failed; verify input and database readiness")

@router.delete("/patient/{patient_id}")
def delete_patient(patient_id: str):
    from app.deletion import delete_patient_records
    return delete_patient_records(patient_id)
