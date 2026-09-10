"""
MongoDB Schema Setup and Initialization

Creates all collections with validators and indexes.
This should be run once during application startup.
"""

import logging
from datetime import datetime
from app.db.mongodb_config import get_database

logger = logging.getLogger(__name__)

# Default org for multi-tenant support
DEFAULT_ORG = "org_default"


def create_collections_and_indexes():
    """
    Initialize MongoDB collections with validators and indexes.
    
    This should be called once during application startup.
    Safe to call multiple times (idempotent).
    
    Collections created:
    1. patient_profiles - Patient master data
    2. care_events - Care events/invoices with nested services
    3. billing_details - Invoice billing information
    4. billing_summary - Monthly billing summaries
    5. care_event_history - Audit trail
    6. entlastungsleistung_tracking - Legacy year-based tracking (unused, kept for reference)
    7. invoice_sequences - Global invoice number counter
    8. entlastung_year_balance - Entlastungsleistung yearly bucket balances

    Raises:
        Exception: If cannot connect to MongoDB or create collections
    """
    db = get_database()

    logger.info("Initializing MongoDB collections and indexes...")

    try:
        # Collection 1: patient_profiles
        _create_patient_profiles_collection(db)

        # Collection 2: care_events
        _create_care_events_collection(db)

        # Collection 3: billing_details
        _create_billing_details_collection(db)

        # Collection 4: billing_summary
        _create_billing_summary_collection(db)

        # Collection 5: care_event_history
        _create_care_event_history_collection(db)

        # Collection 6: entlastungsleistung_tracking
        _create_entlastungsleistung_tracking_collection(db)

        # Collection 7: invoice_sequences
        _create_invoice_sequences_collection(db)

        # Collection 8: entlastung_year_balance
        _create_entlastung_year_balance_collection(db)

        # Collection 9: pdf_audit_documents
        _create_pdf_audit_documents_collection(db)

        # Collection 10: pdf_audit_lines
        _create_pdf_audit_lines_collection(db)
        db.care_events.create_index(
            [("org_id", 1), ("invoicing_month", 1), ("origin_chunk_id", 1)],
            unique=True, partialFilterExpression={"origin_chunk_id": {"$type": "string"}},
            name="idx_import_record_unique")
        db.import_jobs.create_index([("org_id", 1), ("import_id", 1)], unique=True)
        db.service_packet_charges.create_index(
            [("org_id", 1), ("patient_id", 1), ("invoicing_month", 1)], unique=True)
        db.rzh_reconciliation_batches.create_index(
            [("org_id", 1), ("source_pdf_hash", 1)], unique=True,
            name="idx_rzh_batch_source_unique")
        db.rzh_reconciliation_items.create_index(
            [("org_id", 1), ("item_fingerprint", 1)], unique=True,
            name="idx_rzh_item_fingerprint_unique")
        db.rzh_reconciliation_items.create_index(
            [("org_id", 1), ("match_status", 1), ("apply_status", 1)],
            name="idx_rzh_item_review")
        db.entlastung_opening_snapshots.create_index(
            [("org_id", 1), ("patient_id", 1), ("entitlement_year", 1)], unique=True,
            name="idx_entlastung_opening_snapshot_unique")
        db.entlastung_balance_adjustments.create_index(
            [("org_id", 1), ("patient_id", 1), ("entitlement_year", 1)],
            name="idx_entlastung_balance_adjustments_patient")
        db.entlastung_balance_adjustments.create_index(
            [("org_id", 1), ("reconciliation_item_id", 1)], unique=True,
            name="idx_entlastung_balance_adjustments_item_unique")
        db.entlastung_replay_runs.create_index(
            [("org_id", 1), ("patient_id", 1), ("entitlement_year", 1)],
            name="idx_entlastung_replay_runs_patient")

        logger.info("✓ All MongoDB collections and indexes initialized successfully")
        
        # Log summary
        collections = db.list_collection_names()
        logger.info(f"✓ Database contains {len(collections)} collections")
        
        for coll in collections:
            count = db[coll].count_documents({})
            indexes = len(list(db[coll].list_indexes()))
            logger.debug(f"  - {coll}: {count} documents, {indexes} indexes")
        
    except Exception as e:
        logger.error(f"✗ Failed to initialize collections: {e}")
        raise


def _create_patient_profiles_collection(db):
    """Create patient_profiles collection with indexes."""
    coll_name = "patient_profiles"
    
    if coll_name not in db.list_collection_names():
        logger.debug(f"Creating collection: {coll_name}")
        db.create_collection(coll_name)
    
    # Create indexes (sparse=True ignores null values for unique constraint)
    db[coll_name].create_index(
        [("org_id", 1), ("insurance_number", 1)],
        unique=True,
        sparse=True,
        name="idx_org_insurance_unique"
    )
    logger.debug(f"  ✓ Index: org_id + insurance_number (unique, sparse)")
    
    db[coll_name].create_index(
        [("org_id", 1), ("patient_id", 1)],
        unique=True,
        sparse=True,
        name="idx_org_patient_id_unique"
    )
    logger.debug(f"  ✓ Index: org_id + patient_id (unique)")
    
    db[coll_name].create_index(
        [("org_id", 1), ("patient_name", 1)],
        name="idx_org_patient_name"
    )
    logger.debug(f"  ✓ Index: org_id + patient_name")


def _create_care_events_collection(db):
    """Create care_events collection with nested services and indexes."""
    coll_name = "care_events"
    
    if coll_name not in db.list_collection_names():
        logger.debug(f"Creating collection: {coll_name}")
        db.create_collection(coll_name)
    
    # Create indexes
    db[coll_name].create_index(
        [("org_id", 1), ("care_event_id", 1)],
        unique=True,
        sparse=True,
        name="idx_org_care_event_id_unique"
    )
    logger.debug(f"  ✓ Index: org_id + care_event_id (unique)")
    
    db[coll_name].create_index(
        [("org_id", 1), ("patient_id", 1)],
        name="idx_org_patient_id"
    )
    logger.debug(f"  ✓ Index: org_id + patient_id")
    
    db[coll_name].create_index(
        [("org_id", 1), ("event_type", 1)],
        name="idx_org_event_type"
    )
    logger.debug(f"  ✓ Index: org_id + event_type")
    
    db[coll_name].create_index(
        [("org_id", 1), ("period_start_date", 1), ("period_end_date", 1)],
        name="idx_org_period_dates"
    )
    logger.debug(f"  ✓ Index: org_id + period dates (compound)")
    
    db[coll_name].create_index(
        [("org_id", 1), ("care_account", 1)],
        name="idx_org_care_account"
    )
    logger.debug(f"  ✓ Index: org_id + care_account")


def _create_billing_details_collection(db):
    """Create billing_details collection with indexes."""
    coll_name = "billing_details"
    
    if coll_name not in db.list_collection_names():
        logger.debug(f"Creating collection: {coll_name}")
        db.create_collection(coll_name)
    
    # Create indexes
    db[coll_name].create_index(
        [("org_id", 1), ("billing_detail_id", 1)],
        unique=True,
        sparse=True,
        name="idx_org_billing_detail_id_unique"
    )
    logger.debug(f"  ✓ Index: org_id + billing_detail_id (unique, sparse)")
    
    db[coll_name].create_index(
        [("org_id", 1), ("invoicing_month", 1)],
        name="idx_org_invoicing_month"
    )
    logger.debug(f"  ✓ Index: org_id + invoicing_month")
    
    db[coll_name].create_index(
        [("org_id", 1), ("billing_status", 1)],
        name="idx_org_billing_status"
    )
    logger.debug(f"  ✓ Index: org_id + billing_status")
    
    db[coll_name].create_index(
        [("org_id", 1), ("care_event_id", 1)],
        name="idx_org_care_event_id", unique=True
    )
    logger.debug(f"  ✓ Index: org_id + care_event_id")
    
    db[coll_name].create_index(
        [("org_id", 1), ("invoice_number", 1)],
        unique=True,
        partialFilterExpression={"invoice_number": {"$type": "number"}},
        name="idx_org_invoice_number_unique"
    )
    logger.debug(f"  ✓ Index: org_id + month + invoice_number (unique, partial, compound)")


def _create_billing_summary_collection(db):
    """Create billing_summary collection with indexes."""
    coll_name = "billing_summary"
    
    if coll_name not in db.list_collection_names():
        logger.debug(f"Creating collection: {coll_name}")
        db.create_collection(coll_name)
    
    # Create indexes
    db[coll_name].create_index(
        [("org_id", 1), ("billing_month", 1), ("abrechnungsmonat", 1)],
        unique=True,
        partialFilterExpression={"billing_month": {"$type": "string"}},
        name="idx_org_billing_month_unique"
    )
    logger.debug(f"  ✓ Index: org_id + billing_month (unique, partial)")


def _create_care_event_history_collection(db):
    """Create care_event_history collection for audit trail."""
    coll_name = "care_event_history"
    
    if coll_name not in db.list_collection_names():
        logger.debug(f"Creating collection: {coll_name}")
        db.create_collection(coll_name)
    
    # Create indexes
    db[coll_name].create_index(
        [("org_id", 1), ("care_event_id", 1), ("created_at", 1)],
        name="idx_org_event_created"
    )
    logger.debug(f"  ✓ Index: org_id + care_event_id + created_at (compound)")
    
    db[coll_name].create_index(
        [("org_id", 1), ("action", 1), ("created_at", 1)],
        name="idx_org_action_created"
    )
    logger.debug(f"  ✓ Index: org_id + action + created_at (compound)")


def _create_entlastungsleistung_tracking_collection(db):
    """Create entlastungsleistung_tracking collection."""
    coll_name = "entlastungsleistung_tracking"
    
    if coll_name not in db.list_collection_names():
        logger.debug(f"Creating collection: {coll_name}")
        db.create_collection(coll_name)
    
    # Create indexes
    db[coll_name].create_index(
        [("org_id", 1), ("patient_id", 1), ("calendar_year", 1)],
        unique=True,
        sparse=True,
        name="idx_org_patient_year_unique"
    )
    logger.debug(f"  ✓ Index: org_id + patient_id + calendar_year (unique, sparse, compound)")


def _create_entlastung_year_balance_collection(db):
    """Create entlastung_year_balance collection (yearly Entlastungsleistung bucket balances)."""
    coll_name = "entlastung_year_balance"

    if coll_name not in db.list_collection_names():
        logger.debug(f"Creating collection: {coll_name}")
        db.create_collection(coll_name)

    db[coll_name].create_index(
        [("org_id", 1), ("patient_id", 1), ("entitlement_year", 1)],
        unique=True,
        name="idx_org_patient_entitlement_year_unique"
    )
    logger.debug(f"  ✓ Index: org_id + patient_id + entitlement_year (unique, compound)")


def _create_invoice_sequences_collection(db):
    """Create invoice_sequences collection for atomic invoice number counter."""
    coll_name = "invoice_sequences"
    
    if coll_name not in db.list_collection_names():
        logger.debug(f"Creating collection: {coll_name}")
        db.create_collection(coll_name)
    
    # Create indexes
    db[coll_name].create_index(
        [("org_id", 1)],
        unique=True,
        name="idx_org_id_unique"
    )
    logger.debug(f"  ✓ Index: org_id (unique)")
    
    # Initialize default sequence for default org if not exists
    try:
        db[coll_name].update_one(
            {"org_id": DEFAULT_ORG},
            {
                "$setOnInsert": {
                    "org_id": DEFAULT_ORG,
                    "last_number": 0,
                    "created_at": datetime.utcnow()
                }
            },
            upsert=True
        )
        logger.debug(f"  ✓ Initialized sequence counter for org: {DEFAULT_ORG}")
    except Exception as e:
        logger.warning(f"  ⚠ Could not initialize sequence: {e}")


def _create_pdf_audit_documents_collection(db):
    """Create pdf_audit_documents collection (per-PDF audit metadata, independent of care_events)."""
    coll_name = "pdf_audit_documents"

    if coll_name not in db.list_collection_names():
        logger.debug(f"Creating collection: {coll_name}")
        db.create_collection(coll_name)

    db[coll_name].create_index(
        [("org_id", 1), ("year", 1), ("source_file", 1)],
        unique=True,
        name="idx_org_year_source_file_unique"
    )
    logger.debug(f"  ✓ Index: org_id + year + source_file (unique, compound)")


def _create_pdf_audit_lines_collection(db):
    """Create pdf_audit_lines collection (per-service-line audit rows extracted directly from PDFs)."""
    coll_name = "pdf_audit_lines"

    if coll_name not in db.list_collection_names():
        logger.debug(f"Creating collection: {coll_name}")
        db.create_collection(coll_name)

    db[coll_name].create_index(
        [("org_id", 1), ("document_id", 1)],
        name="idx_org_document"
    )
    db[coll_name].create_index(
        [("org_id", 1), ("year", 1), ("billing_month", 1), ("service_code", 1)],
        name="idx_org_year_month_code"
    )
    logger.debug(f"  ✓ Index: org_id + document_id, and org_id + year + billing_month + service_code")


def verify_schema():
    """
    Verify that all required collections exist and have proper indexes.
    
    Returns:
        dict: Schema verification report
    """
    db = get_database()
    
    required_collections = [
        "patient_profiles",
        "care_events",
        "billing_details",
        "billing_summary",
        "care_event_history",
        "entlastungsleistung_tracking",
        "invoice_sequences",
        "rzh_reconciliation_batches",
        "rzh_reconciliation_items",
        "entlastung_opening_snapshots",
        "pdf_audit_documents",
        "pdf_audit_lines",
    ]
    
    existing_collections = db.list_collection_names()
    report = {
        "required": len(required_collections),
        "found": 0,
        "missing": [],
        "collections": {}
    }
    
    for coll in required_collections:
        if coll in existing_collections:
            count = db[coll].count_documents({})
            indexes = len(list(db[coll].list_indexes()))
            report["collections"][coll] = {
                "exists": True,
                "document_count": count,
                "index_count": indexes
            }
            report["found"] += 1
        else:
            report["missing"].append(coll)
            report["collections"][coll] = {"exists": False}
    
    return report


if __name__ == "__main__":
    # Run directly for testing
    import json
    
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    try:
        create_collections_and_indexes()
        print("\n" + "=" * 70)
        report = verify_schema()
        print(json.dumps(report, indent=2))
        print("=" * 70)
    except Exception as e:
        logger.error(f"Setup failed: {e}")
        import sys
        sys.exit(1)
