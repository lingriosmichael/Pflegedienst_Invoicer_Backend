"""
Database migration utilities for schema updates.
Handles migration from old care_records schema (with JSON services) to new schema (separate care_services table).
"""

import json
import logging
from app.db.connection import get_db

logger = logging.getLogger(__name__)

def migrate_care_records_to_services_table():
    """
    Migrate existing care_records with JSON services to the new separate care_services table.
    This is idempotent and safe to run multiple times.
    """
    with get_db() as conn:
        c = conn.cursor()
        
        # Check if care_records still has the old 'services' column
        c.execute("PRAGMA table_info(care_records)")
        columns = {row[1] for row in c.fetchall()}
        
        if 'services' not in columns:
            logger.info("Migration not needed - 'services' column already removed from care_records")
            return
        
        # Check if care_services table exists
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='care_services'")
        if not c.fetchone():
            logger.error("care_services table does not exist. Run init_db() first.")
            return
        
        # Get all care_records with non-null services
        c.execute("""
            SELECT id, services FROM care_records WHERE services IS NOT NULL AND services != ''
        """)
        
        records_to_migrate = c.fetchall()
        if not records_to_migrate:
            logger.info("No care_records with services to migrate")
        else:
            logger.info(f"Found {len(records_to_migrate)} care_records to migrate")
        
        migrated_count = 0
        for record_id, services_json in records_to_migrate:
            try:
                services = json.loads(services_json)
                if not isinstance(services, list):
                    logger.warning(f"Record {record_id}: services is not a list, skipping")
                    continue
                
                # Insert services into care_services table
                for service in services:
                    c.execute("""
                        INSERT INTO care_services 
                        (care_record_id, quantity, code, description, unit_price, total_price)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        record_id,
                        service.get("quantity"),
                        service.get("code"),
                        service.get("description"),
                        service.get("unit_price"),
                        service.get("total_price")
                    ))
                
                migrated_count += 1
            except json.JSONDecodeError as e:
                logger.error(f"Record {record_id}: Failed to parse services JSON: {e}")
            except Exception as e:
                logger.error(f"Record {record_id}: Migration error: {e}")
        
        conn.commit()
        if migrated_count > 0:
            logger.info(f"✓ Migrated {migrated_count} care_records to care_services table")

def migrate_verhinderungspflege_event_types():
    """
    Fix existing Verhinderungspflege records that were incorrectly stored as event_type='SGBV'.
    Updates all records with care_account=4050 to have event_type='Verhinderungspflege'.
    This migration is idempotent and safe to run multiple times.
    """
    with get_db() as conn:
        c = conn.cursor()
        
        try:
            # Check if care_events table exists and has the necessary columns
            c.execute("PRAGMA table_info(care_events)")
            columns = {row[1] for row in c.fetchall()}
            
            if 'event_type' not in columns or 'care_account' not in columns:
                logger.info("care_events table missing expected columns - migration skipped")
                return
            
            # Count records to migrate
            c.execute("""
                SELECT COUNT(*) FROM care_events 
                WHERE care_account = '4050' AND event_type = 'SGBV'
            """)
            count_to_migrate = c.fetchone()[0]
            
            if count_to_migrate == 0:
                logger.info("✓ No Verhinderungspflege records need migration (all already correct)")
                return
            
            # Migrate: Update event_type from SGBV to Verhinderungspflege for all 4050 records
            c.execute("""
                UPDATE care_events 
                SET event_type = 'Verhinderungspflege'
                WHERE care_account = '4050' AND event_type = 'SGBV'
            """)
            
            conn.commit()
            logger.info(f"✓ Fixed {count_to_migrate} Verhinderungspflege records (event_type: SGBV → Verhinderungspflege)")
            
        except Exception as e:
            logger.error(f"Migration failed: {e}")
            conn.rollback()
            raise