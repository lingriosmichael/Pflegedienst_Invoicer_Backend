import logging
import sys
from app.core.config import settings

# Configure Python logging with a sensible default format
def setup_logging():
    """Initialize logging with JSON-friendly format or standard format."""
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    
    # Simple format: timestamp - name - level - message
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    # Configure root logger
    logging.basicConfig(
        level=log_level,
        format=log_format,
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )
    
    return logging.getLogger(__name__)


def get_logger(name: str) -> logging.Logger:
    """Get a logger by module name."""
    return logging.getLogger(name)
