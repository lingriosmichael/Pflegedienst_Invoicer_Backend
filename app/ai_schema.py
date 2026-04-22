"""
AI Schema endpoint for MongoDB database.
Provides schema information for AI reasoning and natural language queries.
"""
from typing import Dict, Any, List
from fastapi import APIRouter
from app.db.mongodb_config import get_database

router = APIRouter(prefix="/ai", tags=["ai"])

# MongoDB collection structure definitions
COLLECTION_SCHEMAS = {
    "patient_profiles": {
        "description": "Patient information including personal and insurance data",
        "fields": {
            "patient_id": {"type": "string", "description": "Unique patient identifier (pat_xxx)"},
            "org_id": {"type": "string", "description": "Organization ID for multi-tenancy"},
            "patient_name": {"type": "string", "description": "Full name of patient"},
            "date_of_birth": {"type": "string", "description": "Birthdate (DD.MM.YYYY)"},
            "insurance_number": {"type": "string", "description": "Health insurance number"},
            "care_level": {"type": "string", "description": "Pflegegrad (1-5)"},
            "include_service_packet": {"type": "integer", "description": "1 if patient has service packet fee"},
            "street_name": {"type": "string", "description": "Street name"},
            "street_number": {"type": "string", "description": "Street number"},
            "postal_code": {"type": "string", "description": "Postal code (PLZ)"},
            "city": {"type": "string", "description": "City name"},
            "debtor_id": {"type": "string", "description": "Debtor number for invoicing"},
        },
        "primary_key": "patient_id",
    },
    "care_events": {
        "description": "Care events/invoices with embedded services",
        "fields": {
            "care_event_id": {"type": "string", "description": "Unique event identifier (evt_xxx)"},
            "org_id": {"type": "string", "description": "Organization ID"},
            "patient_id": {"type": "string", "description": "Reference to patient_profiles.patient_id"},
            "event_type": {"type": "string", "description": "Type: SGBXI, Entleistung, SGBV, Verhinderungspflege, Consultation"},
            "period_start_date": {"type": "string", "description": "Care period start (DD.MM.YY)"},
            "period_end_date": {"type": "string", "description": "Care period end (DD.MM.YY)"},
            "care_account": {"type": "string", "description": "Pflegekonto code (4010, 4064, etc.)"},
            "sum_covered": {"type": "number", "description": "Amount covered by insurance"},
            "sum_total": {"type": "number", "description": "Total invoice amount"},
            "invoicing_month": {"type": "string", "description": "Billing month (MMYYYY)"},
            "services": {"type": "array", "description": "Embedded array of service items"},
        },
        "primary_key": "care_event_id",
        "foreign_keys": [{"field": "patient_id", "references": "patient_profiles.patient_id"}],
    },
    "billing_details": {
        "description": "Billing status and invoice numbers for care events",
        "fields": {
            "billing_detail_id": {"type": "string", "description": "Unique billing ID (bill_xxx)"},
            "org_id": {"type": "string", "description": "Organization ID"},
            "care_event_id": {"type": "string", "description": "Reference to care_events.care_event_id"},
            "invoicing_month": {"type": "string", "description": "Billing month (MMYYYY)"},
            "sum_covered": {"type": "number", "description": "Insurance-covered amount"},
            "sum_total": {"type": "number", "description": "Total amount"},
            "investitionskosten": {"type": "number", "description": "Investment costs (6% for SGBXI)"},
            "amount_owed": {"type": "number", "description": "Amount owed by patient"},
            "invoice_number": {"type": "integer", "description": "Assigned invoice number"},
            "billing_status": {"type": "string", "description": "Status: invoice_needed, covered_insurance, paid"},
        },
        "primary_key": "billing_detail_id",
        "foreign_keys": [{"field": "care_event_id", "references": "care_events.care_event_id"}],
    },
    "billing_summary": {
        "description": "Monthly billing summary aggregates",
        "fields": {
            "org_id": {"type": "string", "description": "Organization ID"},
            "abrechnungsmonat": {"type": "string", "description": "Billing month"},
            "submitted_invoices_count": {"type": "integer", "description": "Number of invoices submitted"},
            "submitted_invoices_amount": {"type": "number", "description": "Total amount submitted"},
        },
        "primary_key": "_id",
    },
}

# Semantic mappings — helps GPT map natural German words to field names
SEMANTIC_HINTS = {
    "patient_profiles": {
        "patient_name": ["Patient", "Name"],
        "date_of_birth": ["Geburtsdatum", "Geburtsdatum des Patienten"],
        "insurance_number": ["Versicherungsnummer", "Kassennummer"],
        "care_level": ["Pflegegrad", "Pflegestufe"],
        "city": ["Stadt", "Wohnort"],
        "debtor_id": ["Debitorennummer", "Debitor"],
    },
    "care_events": {
        "patient_id": ["Patient-ID", "Patientenreferenz"],
        "period_start_date": ["Pflegezeitraum Beginn", "Startdatum"],
        "period_end_date": ["Pflegezeitraum Ende", "Enddatum"],
        "sum_covered": ["Summe übernommen", "Kassenanteil", "Abgedeckt"],
        "sum_total": ["Gesamtsumme", "Gesamtbetrag"],
        "care_account": ["Pflegekonto", "Leistungstyp", "SGBXI", "Entlastungsleistung"],
        "invoicing_month": ["Abrechnungsmonat", "Monat"],
        "event_type": ["Ereignistyp", "Leistungsart"],
    },
    "billing_details": {
        "invoice_number": ["Rechnungsnummer"],
        "amount_owed": ["Betrag offen", "Eigenanteil", "Privatrechnung"],
        "billing_status": ["Abrechnungsstatus", "Status"],
    },
    "services": {
        "service_code": ["Leistungscode", "Kürzel"],
        "service_description": ["Beschreibung", "Leistungsbeschreibung"],
        "quantity_value": ["Anzahl", "Menge"],
        "unit_price": ["Einzelpreis"],
        "line_total": ["Gesamtpreis", "Summe Position"],
    },
}


def _get_sample_documents(collection_name: str, limit: int = 3) -> List[Dict]:
    """Get sample documents from a collection."""
    try:
        db = get_database()
        docs = list(db[collection_name].find({"org_id": "org_default"}).limit(limit))
        # Remove MongoDB _id for cleaner output
        for doc in docs:
            if "_id" in doc:
                del doc["_id"]
        return docs
    except Exception:
        return []


def _get_collection_stats(collection_name: str) -> Dict[str, Any]:
    """Get basic stats for a collection."""
    try:
        db = get_database()
        count = db[collection_name].count_documents({"org_id": "org_default"})
        return {"document_count": count}
    except Exception:
        return {"document_count": 0}


@router.get("/schema")
def get_ai_schema() -> Dict[str, Any]:
    """
    Returns the database schema for AI reasoning.
    Includes:
    - Collections (MongoDB equivalent of tables)
    - Field definitions
    - Relationships
    - Example documents
    - Semantic hints for field names
    """
    collections_map: Dict[str, Any] = {}
    relationships: List[Dict[str, str]] = []

    for coll_name, schema in COLLECTION_SCHEMAS.items():
        samples = _get_sample_documents(coll_name, 3)
        stats = _get_collection_stats(coll_name)
        
        collections_map[coll_name] = {
            "description": schema.get("description", ""),
            "primary_key": schema.get("primary_key"),
            "fields": schema.get("fields", {}),
            "foreign_keys": schema.get("foreign_keys", []),
            "document_examples": samples,
            "stats": stats,
        }
        
        # Build relationships from foreign keys
        for fk in schema.get("foreign_keys", []):
            ref_parts = fk["references"].split(".")
            relationships.append({
                "from_collection": coll_name,
                "from_field": fk["field"],
                "to_collection": ref_parts[0],
                "to_field": ref_parts[1] if len(ref_parts) > 1 else "_id",
            })

    return {
        "db": "mongodb",
        "database_name": "pflegedienst_db",
        "collections": collections_map,
        "relationships": relationships,
        "semantic_hints": SEMANTIC_HINTS,
        "description": "MongoDB database for a German Pflegedienst. Contains patient_profiles, care_events (with embedded services), billing_details, and billing_summary. Relationships: care_events → patient_profiles (N:1), billing_details → care_events (1:1).",
        "query_language": "MongoDB aggregation pipelines (not SQL)",
    }
