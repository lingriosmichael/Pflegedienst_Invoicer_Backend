try:
    # pydantic v2 moved BaseSettings to pydantic-settings package
    from pydantic import BaseSettings
except Exception:
    from pydantic_settings import BaseSettings
from dotenv import load_dotenv
import os
import logging

logger = logging.getLogger(__name__)

# Load .env from project root (for local development). In production prefer real env vars / secret manager.
load_dotenv()


def _get_openai_key():
    """
    Get OpenAI API key from multiple sources in order:
    1. Environment variable (highest priority)
    2. OS keyring (if available)
    3. .env file (if loaded)
    Returns None if not found.
    """
    # Check env var first
    env_key = os.getenv("OPENAI_API_KEY")
    if env_key:
        return env_key
    
    # Try keyring
    try:
        import keyring
        keyring_key = keyring.get_password("pflegedienst-invoicer", "openai_api_key")
        if keyring_key:
            logger.debug("OpenAI key retrieved from OS keyring.")
            return keyring_key
    except ImportError:
        pass  # keyring not installed
    except Exception as e:
        logger.debug(f"Keyring access failed: {e}")
    
    return None


class Settings(BaseSettings):
    OPENAI_API_KEY: str | None = _get_openai_key()
    DB_PATH: str = os.getenv("DB_PATH", "data/invoices.db")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    class Config:
        env_file = ".env"
        extra = "allow"  # Allow extra fields from environment
        env_file_encoding = "utf-8"


settings = Settings()
