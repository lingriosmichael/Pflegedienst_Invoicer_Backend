"""
MongoDB Connection Module

This module provides MongoDB database access for the Pflegedienst Invoicer.

For all database operations, use MongoDB repositories:
    from app.db import PatientRepository
    patient = PatientRepository.find_by_id("P123")
"""

import logging
from contextlib import contextmanager
from app.db.mongodb_config import (
    get_database,
    get_client,
    close_connection,
    health_check,
    get_connection_info
)

logger = logging.getLogger(__name__)

# Backward compatibility: define DB_PATH (MongoDB uses this for dummy reference)
DB_PATH = "mongodb://localhost:27017"  # Reference only, not used by MongoDB

# Re-export for backward compatibility
__all__ = [
    "get_db",
    "get_database",
    "get_client",
    "close_connection",
    "health_check",
    "get_connection_info"
]


@contextmanager
def get_db():
    """
    Context manager for MongoDB database access.
    
    Maintains compatibility with old SQLite get_db() interface.
    
    Usage:
        with get_db() as db:
            collection = db['patient_profiles']
            patient = collection.find_one({"patient_id": "P123"})
    
    Yields:
        Database: MongoDB database instance
        
    Raises:
        Exception: If database operation fails
    """
    db = get_database()
    try:
        yield db
    except Exception as e:
        logger.error(f"Database error: {e}")
        raise


def db_query(query: str = None, params: tuple = None, fetch_one: bool = False):
    """
    DEPRECATED: SQLite-specific legacy function.
    
    This was used for raw SQL queries in SQLite. MongoDB doesn't use SQL.
    
    For MongoDB queries, use repositories instead:
        >>> from app.db import PatientRepository
        >>> patient = PatientRepository.find_by_id("P123")
    
    Raises:
        NotImplementedError: Always, to prevent use in MongoDB code
    """
    raise NotImplementedError(
        "db_query() was SQLite-specific and is no longer supported.\n"
        "Use MongoDB repositories instead:\n"
        "  from app.db import PatientRepository\n"
        "  patient = PatientRepository.find_by_id('P123')"
    )
