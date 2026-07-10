import traceback
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
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
                    "include_service_packet": bool(p.get("include_service_packet", False))
                }
                for p in patients
            ]
        }
    except Exception as e:
        logger.error(f"/patients error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/patient")
def create_patient(req: PatientRequest):
    """Create a new patient in MongoDB."""
    try:
        from app.db.mongodb_repositories import PatientRepository
        from app.db.connection import get_database
        from datetime import datetime
        import time
        
        # Generate a numeric patient_id (unique timestamp-based ID)
        # Get the max patient_id from existing patients and increment
        db = get_database()
        org_id = "org_default"
        
        # Find the highest existing patient_id
        max_patient = db.patient_profiles.find_one(
            {"org_id": org_id},
            sort=[("patient_id", -1)]
        )
        
        if max_patient and max_patient.get("patient_id"):
            if isinstance(max_patient["patient_id"], int):
                next_patient_id = max_patient["patient_id"] + 1
            else:
                # Fallback: use timestamp-based ID if existing IDs are not integers
                next_patient_id = int(time.time() * 1000) % (2**31)
        else:
            # If no patients exist, start from a reasonable number
            next_patient_id = 1000
        
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
            "debtor_id": ""
        }
        
        PatientRepository.create(patient_data, org_id)
        return {"status": "ok", "patient_id": str(next_patient_id)}
    except Exception as e:
        logger.error(f"/patient POST error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

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
                "include_service_packet": bool(patient.get("include_service_packet", False))
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/patient GET error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/patient/{patient_id}")
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
        logger.error(f"/patient PUT error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/patient/{patient_id}")
def delete_patient(patient_id: str):
    """Delete a patient and associated care_events from MongoDB."""
    try:
        from app.db.connection import get_database
        
        db = get_database()
        org_id = "org_default"
        
        logger.info(f"Deleting patient {patient_id}")
        
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
        
        # Find all care_events for this patient and delete related records
        care_events = list(db.care_events.find(
            query,
            {"_id": 0, "care_event_id": 1}
        ))
        
        logger.info(f"Found {len(care_events)} care_events for patient {patient_id}")
        
        for event in care_events:
            evt_id = event.get("care_event_id")
            if evt_id:
                logger.debug(f"Deleting billing/history for event {evt_id}")
                db.billing_details.delete_many({"org_id": org_id, "care_event_id": evt_id})
                db.care_event_history.delete_many({"org_id": org_id, "care_event_id": evt_id})
        
        # Delete all care_events for this patient (using same query)
        deleted_events = db.care_events.delete_many(query)
        logger.info(f"Deleted {deleted_events.deleted_count} care_events")
        
        # Delete the patient (using same query)
        result = db.patient_profiles.delete_one(query)
        
        if result.deleted_count == 0:
            logger.warning(f"Patient {patient_id} not found")
            raise HTTPException(status_code=404, detail="Patient not found")
        
        logger.info(f"✓ Successfully deleted patient {patient_id}")
        return {"status": "ok", "deleted": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/patient DELETE error: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))
