#!/usr/bin/env python3
"""
Backfill invoicing_month from SQLite to MongoDB.

This script:
1. Reads care_events with invoicing_month from SQLite
2. Updates corresponding MongoDB care_events with the invoicing_month field
"""

import sqlite3
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.connection import get_database

SQLITE_PATH = "data/invoices.db"

def backfill_invoicing_month():
    """Backfill invoicing_month from SQLite to MongoDB care_events."""
    
    # Connect to SQLite
    print(f"Connecting to SQLite: {SQLITE_PATH}")
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.row_factory = sqlite3.Row
    cursor = sqlite_conn.cursor()
    
    # Connect to MongoDB
    print("Connecting to MongoDB...")
    db = get_database()
    
    # Get all care_events with invoicing_month from SQLite
    cursor.execute("""
        SELECT care_event_id, invoicing_month 
        FROM care_events 
        WHERE invoicing_month IS NOT NULL
    """)
    sqlite_events = cursor.fetchall()
    print(f"Found {len(sqlite_events)} care_events with invoicing_month in SQLite")
    
    # Track statistics
    updated = 0
    not_found = 0
    already_set = 0
    
    for row in sqlite_events:
        care_event_id = row["care_event_id"]
        invoicing_month = row["invoicing_month"]
        
        # Find in MongoDB
        mongo_event = db.care_events.find_one({"care_event_id": care_event_id})
        
        if not mongo_event:
            not_found += 1
            continue
        
        # Check if already set
        if mongo_event.get("invoicing_month"):
            already_set += 1
            continue
        
        # Update MongoDB
        result = db.care_events.update_one(
            {"care_event_id": care_event_id},
            {"$set": {"invoicing_month": invoicing_month}}
        )
        
        if result.modified_count > 0:
            updated += 1
    
    sqlite_conn.close()
    
    # Print summary
    print("\n" + "=" * 50)
    print("BACKFILL COMPLETE")
    print("=" * 50)
    print(f"SQLite records with invoicing_month: {len(sqlite_events)}")
    print(f"MongoDB records updated:             {updated}")
    print(f"Already had invoicing_month:         {already_set}")
    print(f"Not found in MongoDB:                {not_found}")
    
    # Verify by counting MongoDB records with invoicing_month
    total_mongo = db.care_events.count_documents({})
    with_month = db.care_events.count_documents({"invoicing_month": {"$ne": None}})
    print(f"\nMongoDB care_events total:           {total_mongo}")
    print(f"MongoDB care_events with month:      {with_month}")
    
    return updated

if __name__ == "__main__":
    backfill_invoicing_month()
