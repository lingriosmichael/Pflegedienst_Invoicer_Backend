"""
Database connection management with context manager.
Ensures proper cleanup and error handling.
"""

import sqlite3
import logging
import time
from contextlib import contextmanager
from typing import Generator
from functools import wraps

logger = logging.getLogger(__name__)

DB_PATH = "data/invoices.db"


def retry_on_locked(max_retries=5):
    """
    Decorator to retry database operations if locked.
    Implements exponential backoff.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e) and attempt < max_retries - 1:
                        wait_time = (2 ** attempt) * 0.1  # 0.1s, 0.2s, 0.4s, 0.8s, 1.6s
                        logger.warning(f"Database locked, retrying in {wait_time:.2f}s (attempt {attempt + 1}/{max_retries})")
                        time.sleep(wait_time)
                    else:
                        raise
            return None
        return wrapper
    return decorator


@contextmanager
def get_db() -> Generator:
    """
    Safe database connection context manager.
    
    Automatically handles:
    - Connection creation
    - WAL mode for concurrent access
    - Proper timeout for lock contention
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
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)  # 30 second timeout for locks
    conn.row_factory = sqlite3.Row  # Return rows as dict-like objects
    
    # Set pragmas BEFORE using the connection
    try:
        conn.execute("PRAGMA journal_mode = WAL")  # Write-Ahead Log for concurrent reads/writes
        conn.execute("PRAGMA synchronous = NORMAL")  # Balance safety and performance
        conn.execute("PRAGMA cache_size = -64000")  # 64MB cache
        conn.execute("PRAGMA temp_store = MEMORY")  # Temp tables in memory
        conn.execute("PRAGMA foreign_keys = ON")  # Enforce FKs
        conn.commit()  # Commit pragma changes
    except Exception as pragma_error:
        logger.warning(f"Could not set pragmas: {pragma_error}")
        conn.close()
        raise
    
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
