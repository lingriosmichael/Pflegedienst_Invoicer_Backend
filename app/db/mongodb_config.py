"""
MongoDB Configuration and Connection Management

This module handles:
- Configuration from environment variables
- Connection pooling with MongoClient
- Retry logic for connection issues
- Proper connection cleanup
"""

import os
import logging
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
from pymongo.server_api import ServerApi

# This module reads MONGODB_URI at import time, which can happen before any
# other module has loaded .env (import order across app/ is not guaranteed).
load_dotenv()

logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION FROM ENVIRONMENT
# ============================================================================

MONGODB_URI = os.getenv("MONGODB_URI")
if not MONGODB_URI:
    raise RuntimeError(
        "MONGODB_URI environment variable must be set (no insecure default is provided). "
        "Set it in .env, e.g. mongodb://<user>:<password>@localhost:27017/admin?authSource=admin"
    )
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "pflegedienst_db")

# Connection pooling settings
MAX_POOL_SIZE = int(os.getenv("MONGODB_MAX_POOL_SIZE", "50"))
MIN_POOL_SIZE = int(os.getenv("MONGODB_MIN_POOL_SIZE", "10"))
CONNECTION_TIMEOUT = int(os.getenv("MONGODB_CONNECTION_TIMEOUT", "5000"))
SERVER_SELECTION_TIMEOUT = int(os.getenv("MONGODB_SERVER_SELECTION_TIMEOUT", "10000"))

# Debug mode
DB_DEBUG_LOG = os.getenv("DB_DEBUG_LOG", "false").lower() == "true"

# ============================================================================
# GLOBAL CLIENT INSTANCE
# ============================================================================

_client = None
_database = None


def _get_client() -> MongoClient:
    """
    Get or create MongoDB client with connection pooling.
    
    Uses a singleton pattern to reuse the same client across the application.
    This is important for efficient connection pooling.
    
    Returns:
        MongoClient: Connected MongoDB client instance
        
    Raises:
        ConnectionFailure: If cannot connect to MongoDB after retries
    """
    global _client
    
    if _client is not None:
        return _client
    
    try:
        if DB_DEBUG_LOG:
            logger.debug(f"Connecting to MongoDB: {MONGODB_URI.split('@')[1] if '@' in MONGODB_URI else MONGODB_URI}")
        
        # Create client with connection pooling and timeouts
        _client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT,
            connectTimeoutMS=CONNECTION_TIMEOUT,
            retryWrites=True,
            retryReads=True,
            maxPoolSize=MAX_POOL_SIZE,
            minPoolSize=MIN_POOL_SIZE,
            server_api=ServerApi('1'),  # Use stable API version 1
        )
        
        # Verify connection works
        _client.admin.command('ping')
        logger.info("✓ Connected to MongoDB successfully")
        
        if DB_DEBUG_LOG:
            logger.debug(f"Connection pool - Min: {MIN_POOL_SIZE}, Max: {MAX_POOL_SIZE}")
        
        return _client
        
    except (ConnectionFailure, ServerSelectionTimeoutError) as e:
        logger.error(f"✗ Failed to connect to MongoDB: {e}")
        logger.error(f"  URI: {MONGODB_URI.split('@')[1] if '@' in MONGODB_URI else 'Check MONGODB_URI'}")
        logger.error(f"  Make sure MongoDB is running: docker-compose up -d")
        raise


def get_database():
    """
    Get MongoDB database instance.
    
    Returns:
        Database: MongoDB database instance for pflegedienst_invoicer
        
    Example:
        >>> db = get_database()
        >>> collection = db['patient_profiles']
        >>> patient = collection.find_one({"patient_id": "P123"})
    
    Raises:
        ConnectionFailure: If cannot connect to MongoDB
    """
    global _database
    
    if _database is not None:
        return _database
    
    client = _get_client()
    _database = client[MONGODB_DB_NAME]
    
    if DB_DEBUG_LOG:
        logger.debug(f"Using database: {MONGODB_DB_NAME}")
    
    return _database


def get_client():
    """
    Get MongoDB client instance directly.
    
    Useful for operations that need the client (e.g., sessions, transactions).
    
    Returns:
        MongoClient: MongoDB client instance
    """
    return _get_client()


def close_connection():
    """
    Properly close MongoDB connection.
    
    Should be called during application shutdown.
    
    Usage:
        # In FastAPI app shutdown
        @app.on_event("shutdown")
        async def shutdown_event():
            close_connection()
    """
    global _client, _database
    
    if _client is not None:
        try:
            _client.close()
            logger.info("✓ MongoDB connection closed")
        except Exception as e:
            logger.error(f"Error closing MongoDB connection: {e}")
        finally:
            _client = None
            _database = None


def health_check() -> bool:
    """
    Check if MongoDB is healthy and reachable.
    
    Returns:
        bool: True if MongoDB is healthy, False otherwise
        
    Example:
        >>> if health_check():
        ...     print("MongoDB is ready")
        ... else:
        ...     print("MongoDB is down")
    """
    try:
        client = _get_client()
        client.admin.command('ping')
        logger.debug("✓ MongoDB health check passed")
        return True
    except Exception as e:
        logger.warning(f"✗ MongoDB health check failed: {e}")
        return False


def get_connection_info() -> dict:
    """
    Get information about current MongoDB connection.
    
    Returns:
        dict: Connection information for debugging
        
    Example:
        >>> info = get_connection_info()
        >>> print(f"Database: {info['database']}")
        >>> print(f"Server version: {info['version']}")
    """
    try:
        client = _get_client()
        db = get_database()
        
        server_info = client.server_info()
        
        return {
            "database": MONGODB_DB_NAME,
            "version": server_info.get("version", "unknown"),
            "uri_host": MONGODB_URI.split('@')[-1].split('/')[0] if '@' in MONGODB_URI else "unknown",
            "connection_status": "connected",
            "pool_size": {
                "min": MIN_POOL_SIZE,
                "max": MAX_POOL_SIZE
            }
        }
    except Exception as e:
        return {
            "database": MONGODB_DB_NAME,
            "connection_status": "disconnected",
            "error": str(e)
        }


# ============================================================================
# CONTEXT MANAGER FOR DATABASE ACCESS
# ============================================================================

from contextlib import contextmanager


@contextmanager
def get_db():
    """
    Context manager for database access.
    
    This provides the same interface as the old SQLite connection module
    for minimal code changes during migration.
    
    Usage:
        >>> from app.db.mongodb_config import get_db
        >>> with get_db() as db:
        ...     collection = db['patient_profiles']
        ...     patient = collection.find_one({"patient_id": "P123"})
    
    Yields:
        Database: MongoDB database instance
    """
    db = get_database()
    try:
        yield db
    except Exception as e:
        logger.error(f"Database error: {e}")
        raise
