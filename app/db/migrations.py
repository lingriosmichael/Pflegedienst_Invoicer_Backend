import json
from pathlib import Path

from app.db.mongodb_config import get_database


SCHEMA_VERSION = "20260909_entlastung_reviewed_checkpoints"
VALIDATORS = json.loads(Path(__file__).with_name("validators.json").read_text())


def duplicate_groups(database, collection, fields, match=None):
    pipeline = [{"$match": match or {}}, {"$group": {
        "_id": {field: "$" + field for field in fields}, "count": {"$sum": 1}
    }}, {"$match": {"count": {"$gt": 1}}}, {"$count": "groups"}]
    rows = list(database[collection].aggregate(pipeline))
    return rows[0]["groups"] if rows else 0


def preflight(database):
    checks = {
        "duplicate_billing_events": duplicate_groups(database, "billing_details", ["org_id", "care_event_id"]),
        "duplicate_invoice_numbers": duplicate_groups(database, "billing_details", ["org_id", "invoice_number"], {"invoice_number": {"$type": "number"}}),
        "duplicate_import_records": duplicate_groups(database, "care_events", ["org_id", "invoicing_month", "origin_chunk_id"], {"origin_chunk_id": {"$type": "string"}}),
    }
    for collection, validator in VALIDATORS.items():
        checks["invalid_" + collection] = database[collection].count_documents({"$nor": [validator]})
    return checks


def migrate(apply=False):
    database = get_database()
    report = preflight(database)
    if not apply:
        return report
    if any(report.values()):
        raise ValueError("Migration preflight failed; reconcile the reported groups before applying")
    for collection, validator in VALIDATORS.items():
        if collection not in database.list_collection_names():
            database.create_collection(collection, validator=validator)
        else:
            database.command("collMod", collection, validator=validator, validationLevel="strict", validationAction="error")
    existing = database.billing_details.index_information()
    for name in ("idx_org_care_event_id", "idx_org_month_invoice_number_unique"):
        if name in existing:
            database.billing_details.drop_index(name)
    from app.db.mongodb_schema import create_collections_and_indexes

    create_collections_and_indexes()
    database.schema_migrations.update_one({"version": SCHEMA_VERSION}, {"$set": {"version": SCHEMA_VERSION}}, upsert=True)
    return report


def require_ready_database():
    database = get_database()
    hello = database.command("hello")
    if not hello.get("setName") and hello.get("msg") != "isdbgrid":
        raise RuntimeError("MongoDB replica set required for accounting transactions")
    if not database.schema_migrations.find_one({"version": SCHEMA_VERSION}):
        raise RuntimeError("Run scripts/migrate_accounting.py --apply before starting the backend")
