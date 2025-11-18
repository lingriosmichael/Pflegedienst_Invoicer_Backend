"""
Database connection management with context manager.
Ensures proper cleanup and error handling.
"""

import sqlite3
import logging
from contextlib import contextmanager
from typing import Generator

logger = logging.getLogger(__name__)

DB_PATH = "data/invoices.db"


@contextmanager
def get_db() -> Generator:
    """
    Safe database connection context manager.
    
    Automatically handles:
    - Connection creation
    - Foreign key enforcement
    - Transaction rollback on error
    - Connection cleanup
    
    Usage:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM invoices")
            conn.commit()
    
    Raises:
        sqlite3.Error: If database operation fails
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")  # Enforce FKs
    
    try:
        yield conn
    except Exception as e:
        conn.rollback()
        logger.error(f"Database error (rolled back): {e}")
        raise
    finally:
        conn.close()


def db_query(query: str, params: tuple = None, fetch_one: bool = False):
    """
    Helper for simple read-only queries.
    
    Args:
        query: SQL query string
        params: Query parameters tuple
        fetch_one: If True, return single row; else return all rows
    
    Returns:
        Single row (dict-like) or list of rows
    """
    with get_db() as conn:
        conn.row_factory = sqlite3.Row  # Return rows as dicts
        c = conn.cursor()
        c.execute(query, params or ())
        
        if fetch_one:
            return c.fetchone()
        else:
            return c.fetchall()
