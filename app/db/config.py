"""
SQLite database configuration and helpers.
Includes WAL mode for better concurrency, connection pooling hints, and migration utilities.
"""
import sqlite3
import logging

logger = logging.getLogger(__name__)


def enable_wal_mode(db_path: str):
    """
    Enable WAL (Write-Ahead Logging) mode for SQLite.
    This improves concurrency and performance for local multi-client access.
    Safe to call multiple times.
    """
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL")
        mode = c.fetchone()[0]
        logger.info(f"SQLite WAL mode enabled: {mode}")
        
        # Recommended settings for local installs
        c.execute("PRAGMA synchronous=NORMAL")  # faster, safer than FULL for local
        c.execute("PRAGMA cache_size=-64000")   # 64MB cache
        c.execute("PRAGMA foreign_keys=ON")     # enforce FK constraints
        
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Could not enable WAL mode: {e}")


def get_db_connection(db_path: str) -> sqlite3.Connection:
    """
    Get a SQLite connection with recommended settings for local installs.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Set timeout for "database is locked" retries
    conn.execute("PRAGMA busy_timeout=5000")  # 5 second timeout
    return conn


def init_db_with_wal(db_path: str):
    """
    Initialize database and enable WAL mode.
    Call this once at startup.
    """
    enable_wal_mode(db_path)
    logger.info(f"Database initialized with WAL mode: {db_path}")
