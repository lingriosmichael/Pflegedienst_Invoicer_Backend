"""
MongoDB Repository Layer for Pflegedienst Invoicer

Replaces SQLite repositories with MongoDB implementations.
Maintains the same interface for backward compatibility while using MongoDB's document model.

Key patterns:
- org_id for multi-tenancy (required in most queries)
- Atomic operations (especially invoice number generation)
- Embedded documents (services within care_events)
- Aggregation pipelines for complex queries
"""

import logging
from datetime import datetime
from typing import Optional, Dict, List, Any
from bson import ObjectId
from pymongo.errors import DuplicateKeyError
from app.db.mongodb_config import get_database
from app.exceptions import InvoiceNotFoundError, PatientNotFoundError

logger = logging.getLogger(__name__)

# Default organization ID for single-tenant deployments
DEFAULT_ORG_ID = "org_default"


def get_org_id(org_id: Optional[str] = None) -> str:
    """Get org_id, using default if not provided."""
    return org_id or DEFAULT_ORG_ID


class InvoiceRepository:
    """Manages all invoice-related database operations in MongoDB.
    
    Invoice data is spread across 3 collections:
    - care_events: Core event data with nested services
    - billing_details: Billing information including invoice_number
    - invoice_sequences: Global counter for atomic invoice number generation
    """
    
    @staticmethod
    def get_next_invoice_number(org_id: Optional[str] = None) -> int:
        """
        Atomically get the next invoice number.
        
        Uses MongoDB's findOneAndUpdate with upsert to ensure atomic increment.
        This is safe under concurrent load.
        
        Args:
            org_id: Organization ID (defaults to DEFAULT_ORG_ID)
            
        Returns:
            Next sequential invoice number for the organization
            
        Raises:
            Exception: If MongoDB operation fails
        """
        org_id = get_org_id(org_id)
        db = get_database()
        
        try:
            # Atomic increment with upsert (creates if doesn't exist, increments if does)
            result = db.invoice_sequences.find_one_and_update(
                {"org_id": org_id},
                {"$inc": {"last_number": 1}},
                upsert=True,
                return_document=True
            )
            
            next_number = result.get("last_number", 1)
            logger.info(f"Generated invoice number {next_number} for org {org_id}")
            return next_number
            
        except Exception as e:
            logger.error("Operation failed (%s)", type(e).__name__)
            raise

    @staticmethod
    def find_by_month(month: str, org_id: Optional[str] = None, private_only: bool = False) -> List[int]:
        """
        Get all invoice IDs for a given month.
        
        Queries billing_details collection since it has the month field.
        Returns care_event IDs for lookup in care_events collection.
        
        Args:
            month: Invoicing month (format: "YYYY-MM")
            org_id: Organization ID
            private_only: If True, only return private invoices (private_rechnung == 'invoice_needed')
            
        Returns:
            List of care_event_id (which serve as invoice IDs)
        """
        org_id = get_org_id(org_id)
        db = get_database()
        
        query = {"org_id": org_id, "billing_month": month}
        
        if private_only:
            query["private_rechnung"] = "invoice_needed"
        
        results = db.billing_details.find(query, {"care_event_id": 1})
        invoice_ids = [doc["care_event_id"] for doc in results]
        
        logger.debug(f"Found {len(invoice_ids)} invoices for month {month}, org {org_id}")
        return invoice_ids

    @staticmethod
    def find_by_id(invoice_id: int, org_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Get complete invoice record by ID.
        
        Combines data from care_events and billing_details collections.
        
        Args:
            invoice_id: The care_event_id (serves as invoice_id)
            org_id: Organization ID
            
        Returns:
            Dictionary with complete invoice data
            
        Raises:
            InvoiceNotFoundError: If invoice not found
        """
        org_id = get_org_id(org_id)
        db = get_database()
        
        # Get care_event (main invoice data)
        care_event = db.care_events.find_one({
            "org_id": org_id,
            "care_event_id": invoice_id
        })
        
        if not care_event:
            raise InvoiceNotFoundError(f"Invoice {invoice_id} not found for org {org_id}")
        
        # Get billing details
        billing = db.billing_details.find_one({
            "org_id": org_id,
            "care_event_id": invoice_id
        })
        
        # Merge data
        invoice = dict(care_event)
        if billing:
            invoice.update(billing)
        
        # Ensure invoice_id is accessible
        if "_id" in invoice:
            del invoice["_id"]
        invoice["invoice_id"] = invoice_id
        
        return invoice

    @staticmethod
    def find_all(org_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all invoices for an organization."""
        org_id = get_org_id(org_id)
        db = get_database()
        
        result = list(db.care_events.find({"org_id": org_id}))
        
        for doc in result:
            if "_id" in doc:
                del doc["_id"]
        
        return result

    @staticmethod
    def update_invoice_number(invoice_id: int, invoice_number: int, org_id: Optional[str] = None) -> None:
        """
        Atomically assign invoice number to an invoice.
        
        Updates the billing_details document with the generated invoice number.
        
        Args:
            invoice_id: The care_event_id
            invoice_number: The generated invoice number
            org_id: Organization ID
        """
        org_id = get_org_id(org_id)
        db = get_database()
        
        result = db.billing_details.update_one(
            {"org_id": org_id, "care_event_id": invoice_id},
            {"$set": {"invoice_number": invoice_number, "updated_at": datetime.utcnow()}}
        )
        
        if result.matched_count == 0:
            logger.warning(f"Billing record not found for invoice {invoice_id}")
        
        logger.info(f"Assigned invoice number {invoice_number} to invoice {invoice_id}, org {org_id}")

    @staticmethod
    def count_by_month(month: str, org_id: Optional[str] = None) -> int:
        """Count invoices for a given month."""
        org_id = get_org_id(org_id)
        db = get_database()
        
        count = db.billing_details.count_documents({
            "org_id": org_id,
            "billing_month": month
        })
        
        return count

    @staticmethod
    def find_by_patient_id(patient_id: int, org_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all invoices for a specific patient."""
        org_id = get_org_id(org_id)
        db = get_database()
        
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
        
        results = list(db.care_events.find(
            query,
            sort=[("period_end_date", -1)]
        ))
        
        for doc in results:
            if "_id" in doc:
                del doc["_id"]
        
        return results


class PatientRepository:
    """Manages all patient-related database operations."""
    
    @staticmethod
    def find_by_id(patient_id, org_id: Optional[str] = None) -> Dict[str, Any]:
        """Get patient by ID (can be int or string)."""
        org_id = get_org_id(org_id)
        db = get_database()
        
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
        
        patient = db.patient_profiles.find_one(query)
        
        if not patient:
            raise PatientNotFoundError(f"Patient {patient_id} not found for org {org_id}")
        
        if "_id" in patient:
            del patient["_id"]
        
        return patient

    @staticmethod
    def find_by_insurance_number(insurance_number: str, org_id: Optional[str] = None) -> Dict[str, Any]:
        """Get patient by insurance/Krankenkasse number."""
        org_id = get_org_id(org_id)
        db = get_database()
        
        patient = db.patient_profiles.find_one({
            "org_id": org_id,
            "insurance_number": insurance_number
        })
        
        if not patient:
            raise PatientNotFoundError(f"Patient with insurance number {insurance_number}")
        
        if "_id" in patient:
            del patient["_id"]
        
        return patient

    @staticmethod
    def find_all(org_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all patients for an organization."""
        org_id = get_org_id(org_id)
        db = get_database()
        
        results = list(db.patient_profiles.find(
            {"org_id": org_id},
            sort=[("patient_name", 1)]
        ))
        
        for doc in results:
            if "_id" in doc:
                del doc["_id"]
        
        return results

    @staticmethod
    def update_field(patient_id: int, field: str, value: Any, org_id: Optional[str] = None) -> None:
        """Update a single patient field."""
        allowed_fields = {
            "patient_name", "birthdate", "care_level", "address", 
            "debtor_number", "include_service_packet", "insurance_number"
        }
        
        if field not in allowed_fields:
            raise ValueError(f"Cannot update field '{field}'")
        
        org_id = get_org_id(org_id)
        db = get_database()
        
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
        
        result = db.patient_profiles.update_one(
            query,
            {"$set": {field: value, "updated_at": datetime.utcnow()}}
        )
        
        if result.matched_count == 0:
            logger.warning(f"Patient {patient_id} not found for update")
        
        logger.info(f"Updated patient {patient_id} field '{field}'")

    @staticmethod
    def create(patient_data: Dict[str, Any], org_id: Optional[str] = None) -> int:
        """
        Create a new patient.
        
        Args:
            patient_data: Dictionary with patient information
            org_id: Organization ID
            
        Returns:
            The patient_id of the created patient
        """
        org_id = get_org_id(org_id)
        db = get_database()
        
        # Ensure required fields
        patient_data["org_id"] = org_id
        patient_data["created_at"] = datetime.utcnow()
        patient_data["updated_at"] = datetime.utcnow()
        
        try:
            result = db.patient_profiles.insert_one(patient_data)
            patient_id = patient_data.get("patient_id")
            logger.info(f"Created patient {patient_id}")
            return patient_id
        except DuplicateKeyError as e:
            logger.error("Operation failed (%s)", type(e).__name__)
            raise

    @staticmethod
    def count(org_id: Optional[str] = None) -> int:
        """Total patient count."""
        org_id = get_org_id(org_id)
        db = get_database()
        
        count = db.patient_profiles.count_documents({"org_id": org_id})
        return count

    @staticmethod
    def find_missing_fields(month: str, org_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Find patients with incomplete data in a specific invoicing month.
        
        Uses MongoDB aggregation to find patients with missing address or debtor_number
        who have invoices in the given month.
        
        Args:
            month: Invoicing month (format: "YYYY-MM")
            org_id: Organization ID
            
        Returns:
            List of patient documents with incomplete data
        """
        org_id = get_org_id(org_id)
        db = get_database()
        
        # Aggregation pipeline to find patients with missing fields
        pipeline = [
            # Match billing records for the month
            {
                "$match": {
                    "org_id": org_id,
                    "billing_month": month
                }
            },
            # Get unique patient IDs
            {
                "$group": {
                    "_id": "$patient_id"
                }
            },
            # Join with patient_profiles
            {
                "$lookup": {
                    "from": "patient_profiles",
                    "let": {"patient_id": "$_id"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$patient_id", "$$patient_id"]},
                                        {"$eq": ["$org_id", org_id]}
                                    ]
                                }
                            }
                        },
                        # Filter for missing fields
                        {
                            "$match": {
                                "$or": [
                                    {"address": {"$in": [None, ""]}},
                                    {"debtor_number": {"$in": [None, ""]}}
                                ]
                            }
                        }
                    ],
                    "as": "patient"
                }
            },
            # Unwind patient array (should have 0 or 1 element)
            {
                "$unwind": {"path": "$patient", "preserveNullAndEmptyArrays": False}
            },
            # Project clean document
            {
                "$project": {
                    "_id": 0,
                    "patient_id": "$patient.patient_id",
                    "patient_name": "$patient.patient_name",
                    "address": "$patient.address",
                    "debtor_number": "$patient.debtor_number"
                }
            }
        ]
        
        results = list(db.billing_details.aggregate(pipeline))
        return results


class CareEventRepository:
    """Manages care events (invoices with services) in MongoDB.
    
    Care events are the main transaction records, with services embedded as subdocuments.
    This replaces both invoices and care_records from the SQLite schema.
    """
    
    @staticmethod
    def create(care_event_data: Dict[str, Any], org_id: Optional[str] = None) -> int:
        """
        Create a new care event with nested services.
        
        Args:
            care_event_data: Dictionary containing:
                - care_event_id: Unique event ID
                - patient_id: Patient reference
                - event_type: Type of care event
                - period_start_date, period_end_date: Event period
                - services: List of service subdocuments
                - Other metadata
            org_id: Organization ID
            
        Returns:
            The care_event_id
        """
        org_id = get_org_id(org_id)
        db = get_database()
        
        care_event_data["org_id"] = org_id
        care_event_data["created_at"] = datetime.utcnow()
        care_event_data["updated_at"] = datetime.utcnow()
        
        # Ensure services is a list
        if "services" not in care_event_data:
            care_event_data["services"] = []
        
        try:
            db.care_events.insert_one(care_event_data)
            care_event_id = care_event_data.get("care_event_id")
            logger.info(f"Created care event {care_event_id} with {len(care_event_data['services'])} services")
            return care_event_id
        except DuplicateKeyError as e:
            logger.error("Operation failed (%s)", type(e).__name__)
            raise

    @staticmethod
    def find_by_id(care_event_id: int, org_id: Optional[str] = None) -> Dict[str, Any]:
        """Get a care event by ID."""
        org_id = get_org_id(org_id)
        db = get_database()
        
        event = db.care_events.find_one({
            "org_id": org_id,
            "care_event_id": care_event_id
        })
        
        if not event:
            raise InvoiceNotFoundError(f"Care event {care_event_id}")
        
        if "_id" in event:
            del event["_id"]
        
        return event

    @staticmethod
    def find_by_patient_id(patient_id: int, org_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all care events for a patient."""
        org_id = get_org_id(org_id)
        db = get_database()
        
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
        
        results = list(db.care_events.find(
            query,
            sort=[("period_end_date", -1)]
        ))
        
        for doc in results:
            if "_id" in doc:
                del doc["_id"]
        
        return results

    @staticmethod
    def find_by_event_type(event_type: str, org_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all care events of a specific type."""
        org_id = get_org_id(org_id)
        db = get_database()
        
        results = list(db.care_events.find({
            "org_id": org_id,
            "event_type": event_type
        }))
        
        for doc in results:
            if "_id" in doc:
                del doc["_id"]
        
        return results

    @staticmethod
    def find_by_month(start_date: str, end_date: str, org_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get all care events in a date range.
        
        Args:
            start_date: ISO format date string
            end_date: ISO format date string
            org_id: Organization ID
            
        Returns:
            List of care events in the date range
        """
        org_id = get_org_id(org_id)
        db = get_database()
        
        results = list(db.care_events.find({
            "org_id": org_id,
            "period_start_date": {"$gte": start_date},
            "period_end_date": {"$lte": end_date}
        }))
        
        for doc in results:
            if "_id" in doc:
                del doc["_id"]
        
        return results

    @staticmethod
    def update(care_event_id: int, updates: Dict[str, Any], org_id: Optional[str] = None) -> None:
        """Update a care event."""
        org_id = get_org_id(org_id)
        db = get_database()
        
        updates["updated_at"] = datetime.utcnow()
        
        result = db.care_events.update_one(
            {"org_id": org_id, "care_event_id": care_event_id},
            {"$set": updates}
        )
        
        if result.matched_count == 0:
            logger.warning(f"Care event {care_event_id} not found for update")
        
        logger.info(f"Updated care event {care_event_id}")

    @staticmethod
    def add_service(care_event_id: int, service_data: Dict[str, Any], org_id: Optional[str] = None) -> None:
        """Add a service to a care event."""
        org_id = get_org_id(org_id)
        db = get_database()
        
        result = db.care_events.update_one(
            {"org_id": org_id, "care_event_id": care_event_id},
            {
                "$push": {"services": service_data},
                "$set": {"updated_at": datetime.utcnow()}
            }
        )
        
        if result.matched_count == 0:
            logger.warning(f"Care event {care_event_id} not found")
        
        logger.info(f"Added service to care event {care_event_id}")

    @staticmethod
    def get_summary_by_event_type(event_type: str, org_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get care events grouped by date for a specific event type.
        
        Used by analytics endpoints for dashboard visualization.
        
        Args:
            event_type: Type of event (e.g., "SGBV", "SGBXI", "Verhinderungspflege", "Consultation")
            org_id: Organization ID
            
        Returns:
            List of dicts with keys:
                - _id: The date (period_start_date)
                - record_count: Number of events on that date
                - total_amount: Sum of amounts on that date
                
        Example:
            data = CareEventRepository.get_summary_by_event_type("SGBV")
            # Returns: [
            #     {"_id": "01.01.25", "record_count": 5, "total_amount": 150.00},
            #     {"_id": "02.01.25", "record_count": 3, "total_amount": 90.00}
            # ]
        """
        org_id = get_org_id(org_id)
        db = get_database()
        
        pipeline = [
            {
                "$match": {
                    "org_id": org_id,
                    "event_type": event_type
                }
            },
            {
                "$group": {
                    "_id": "$period_start_date",
                    "record_count": {"$sum": 1},
                    "total_amount": {"$sum": {"$toDouble": "$sum_total"}}
                }
            },
            {"$sort": {"_id": 1}}
        ]
        
        try:
            results = list(db.care_events.aggregate(pipeline))
            logger.info(f"get_summary_by_event_type({event_type}): {len(results)} date groups")
            return results
        except Exception as e:
            logger.error("Operation failed (%s)", type(e).__name__)
            raise

    @staticmethod
    def get_patient_histogram(patient_id: str, event_types: List[str] = None, 
                            org_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get breakdown of patient invoices by type and month.
        
        Used for patient histogram charts in chart_generator.py
        
        Args:
            patient_id: Patient ID to get data for
            event_types: List of event types to include (default: all)
            org_id: Organization ID
            
        Returns:
            List of dicts grouped by event_type and date:
                - _id.event_type: The event type
                - _id.date: The period_start_date
                - monthly_total: Sum of amounts for that type/date
                - record_count: Number of records
                
        Example:
            data = CareEventRepository.get_patient_histogram("pat_123")
            # Returns: [
            #     {"_id": {"event_type": "SGBXI", "date": "01.01.25"}, "monthly_total": 100.00, "record_count": 1},
            #     {"_id": {"event_type": "Entleistung", "date": "01.01.25"}, "monthly_total": 50.00, "record_count": 1}
            # ]
        """
        org_id = get_org_id(org_id)
        db = get_database()
        
        # Try both string and integer formats to handle mixed ID types in DB
        patient_id_int = None
        try:
            patient_id_int = int(patient_id)
        except (ValueError, TypeError):
            pass
        
        # Build match filter with both possible ID formats
        match_filter = {"org_id": org_id}
        if patient_id_int is not None:
            match_filter["patient_id"] = {"$in": [patient_id, patient_id_int]}
        else:
            match_filter["patient_id"] = patient_id
        
        if event_types:
            match_filter["event_type"] = {"$in": event_types}
        
        pipeline = [
            {"$match": match_filter},
            {
                "$group": {
                    "_id": {
                        "event_type": "$event_type",
                        "date": "$period_start_date"
                    },
                    "monthly_total": {"$sum": {"$toDouble": "$sum_total"}},
                    "record_count": {"$sum": 1}
                }
            },
            {"$sort": {"_id.date": 1}}
        ]
        
        try:
            results = list(db.care_events.aggregate(pipeline))
            logger.info(f"get_patient_histogram({patient_id}): {len(results)} type/date groups")
            return results
        except Exception as e:
            logger.error("Operation failed (%s)", type(e).__name__)
            raise

    @staticmethod
    def aggregate_by_event_type(org_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Get total amounts by event type across all records.
        
        Used for pie charts and summaries.
        
        Args:
            org_id: Organization ID
            
        Returns:
            List of dicts with _id (event_type) and total_amount
            
        Example:
            data = CareEventRepository.aggregate_by_event_type()
            # Returns: [
            #     {"_id": "SGBXI", "total_amount": 5000.00, "record_count": 50},
            #     {"_id": "SGBV", "total_amount": 3000.00, "record_count": 30}
            # ]
        """
        org_id = get_org_id(org_id)
        db = get_database()
        
        pipeline = [
            {"$match": {"org_id": org_id}},
            {
                "$group": {
                    "_id": "$event_type",
                    "total_amount": {"$sum": {"$toDouble": "$sum_total"}},
                    "record_count": {"$sum": 1}
                }
            },
            {"$sort": {"total_amount": -1}}
        ]
        
        try:
            results = list(db.care_events.aggregate(pipeline))
            logger.info(f"aggregate_by_event_type: {len(results)} event types")
            return results
        except Exception as e:
            logger.error("Operation failed (%s)", type(e).__name__)
            raise


class ServiceRepository:
    """Manages service/Leistung operations.
    
    Note: Services are now embedded within care_events documents.
    This repository provides convenience methods for service queries.
    """
    
    @staticmethod
    def find_by_care_event_id(care_event_id: int, org_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get all services for a care event.
        
        Args:
            care_event_id: The care event ID
            org_id: Organization ID
            
        Returns:
            List of service documents
        """
        org_id = get_org_id(org_id)
        db = get_database()
        
        event = db.care_events.find_one(
            {"org_id": org_id, "care_event_id": care_event_id},
            {"services": 1}
        )
        
        if not event:
            return []
        
        return event.get("services", [])

    @staticmethod
    def find_all_codes(org_id: Optional[str] = None) -> List[str]:
        """Get all unique service codes in the system."""
        org_id = get_org_id(org_id)
        db = get_database()
        
        # Aggregation to get unique service codes
        pipeline = [
            {"$match": {"org_id": org_id}},
            {"$unwind": "$services"},
            {"$group": {"_id": "$services.code"}},
            {"$sort": {"_id": 1}}
        ]
        
        results = db.care_events.aggregate(pipeline)
        codes = [doc["_id"] for doc in results if doc["_id"]]
        
        return codes

    @staticmethod
    def count_total(org_id: Optional[str] = None) -> int:
        """Total service line items in system."""
        org_id = get_org_id(org_id)
        db = get_database()
        
        # Aggregation to count all services across all events
        pipeline = [
            {"$match": {"org_id": org_id}},
            {"$unwind": "$services"},
            {"$count": "total"}
        ]
        
        results = list(db.care_events.aggregate(pipeline))
        
        if results:
            return results[0]["total"]
        return 0


class BillingRepository:
    """Manages billing-related operations in MongoDB."""
    
    @staticmethod
    def create_billing_details(billing_data: Dict[str, Any], org_id: Optional[str] = None) -> str:
        """
        Create a billing details record.
        
        Args:
            billing_data: Dictionary with billing information
            org_id: Organization ID
            
        Returns:
            MongoDB ObjectId as string
        """
        org_id = get_org_id(org_id)
        db = get_database()
        
        billing_data["org_id"] = org_id
        billing_data["created_at"] = datetime.utcnow()
        billing_data["updated_at"] = datetime.utcnow()
        
        result = db.billing_details.insert_one(billing_data)
        logger.info(f"Created billing record for care_event {billing_data.get('care_event_id')}")
        return str(result.inserted_id)

    @staticmethod
    def find_by_month(month: str, org_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all billing records for a month."""
        org_id = get_org_id(org_id)
        db = get_database()
        
        results = list(db.billing_details.find({
            "org_id": org_id,
            "billing_month": month
        }))
        
        for doc in results:
            if "_id" in doc:
                doc["_id"] = str(doc["_id"])
        
        return results

    @staticmethod
    def find_by_status(status: str, org_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all billing records with a specific status."""
        org_id = get_org_id(org_id)
        db = get_database()
        
        results = list(db.billing_details.find({
            "org_id": org_id,
            "status": status
        }))
        
        for doc in results:
            if "_id" in doc:
                doc["_id"] = str(doc["_id"])
        
        return results

    @staticmethod
    def update_status(billing_id: str, status: str, org_id: Optional[str] = None) -> None:
        """Update billing status."""
        org_id = get_org_id(org_id)
        db = get_database()
        
        db.billing_details.update_one(
            {"_id": ObjectId(billing_id), "org_id": org_id},
            {"$set": {"status": status, "updated_at": datetime.utcnow()}}
        )
        
        logger.info(f"Updated billing {billing_id} status to {status}")

    @staticmethod
    def get_summary_by_month(org_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get billing summary by month using aggregation.
        
        Returns aggregated billing information grouped by month.
        """
        org_id = get_org_id(org_id)
        db = get_database()
        
        pipeline = [
            {"$match": {"org_id": org_id}},
            {
                "$group": {
                    "_id": "$billing_month",
                    "count": {"$sum": 1},
                    "total_amount": {"$sum": {"$toDouble": "$total_amount"}}
                }
            },
            {"$sort": {"_id": -1}}
        ]
        
        results = list(db.billing_details.aggregate(pipeline))
        
        for doc in results:
            if "_id" in doc:
                doc["month"] = doc.pop("_id")
        
        return results


class BillingSummaryRepository:
    """Repository for billing summary operations."""
    
    @staticmethod
    def insert(abrechnungsmonat: str, submitted_invoices_count: int, 
               submitted_invoices_amount: float, org_id: Optional[str] = None) -> str:
        """
        Insert or aggregate billing summary.
        
        Accumulates counts/amounts for the same month across multiple PDFs.
        
        Args:
            abrechnungsmonat: Month in format "YYYY-MM"
            submitted_invoices_count: Count of invoices
            submitted_invoices_amount: Total amount
            org_id: Organization ID
            
        Returns:
            MongoDB ObjectId as string
        """
        org_id = get_org_id(org_id)
        db = get_database()
        
        # Use find_one_and_update to atomically update or insert
        result = db.billing_summary.find_one_and_update(
            {"org_id": org_id, "abrechnungsmonat": abrechnungsmonat},
            {
                "$inc": {
                    "submitted_invoices_count": submitted_invoices_count,
                    "submitted_invoices_amount": submitted_invoices_amount
                },
                "$set": {"updated_at": datetime.utcnow()}
            },
            upsert=True,
            return_document=True
        )
        
        logger.info(f"Upserted billing summary for {abrechnungsmonat}, org {org_id}")
        return str(result["_id"])

    @staticmethod
    def find_by_month(abrechnungsmonat: str, org_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Find billing summary by month."""
        org_id = get_org_id(org_id)
        db = get_database()
        
        doc = db.billing_summary.find_one({
            "org_id": org_id,
            "abrechnungsmonat": abrechnungsmonat
        })
        
        if doc and "_id" in doc:
            doc["_id"] = str(doc["_id"])
        
        return doc

    @staticmethod
    def get_all(org_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieve all billing summaries."""
        org_id = get_org_id(org_id)
        db = get_database()
        
        results = list(db.billing_summary.find(
            {"org_id": org_id},
            sort=[("abrechnungsmonat", -1)]
        ))
        
        for doc in results:
            if "_id" in doc:
                doc["_id"] = str(doc["_id"])
        
        return results
