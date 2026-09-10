import os
import json
import hashlib
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Cache directories
FAILURE_CACHE_DIR = "cache/openai/failures"
os.makedirs(FAILURE_CACHE_DIR, exist_ok=True)


def _failure_cache_path(model: str, system_prompt: str, user_text: str) -> str:
    """Generate a descriptive cache path for failed requests."""
    # Create hash for uniqueness
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    h.update(b"\n--SYSTEM--\n")
    h.update(system_prompt.encode("utf-8"))
    h.update(b"\n--USER--\n")
    h.update(user_text.encode("utf-8"))
    hash_key = h.hexdigest()[:8]  # Use first 8 chars for brevity
    
    # Create readable filename with timestamp and hash
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"failure_{timestamp}_{hash_key}.json"
    return os.path.join(FAILURE_CACHE_DIR, filename)


def make_cache_key(model: str, system_prompt: str, user_text: str) -> str:
    """Create a stable sha256 cache key for the request."""
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    h.update(b"\n--SYSTEM--\n")
    h.update(system_prompt.encode("utf-8"))
    h.update(b"\n--USER--\n")
    h.update(user_text.encode("utf-8"))
    return h.hexdigest()


def cache_failed_request(model: str, system_prompt: str, user_text: str, error: str):
    logger.warning("Extraction failed; retry through the identified import job")


def count_tokens(text: str, model: str = "gpt-5-mini-2025-08-07") -> int:
    """
    Count tokens for a model. Prefer `tiktoken` if installed, otherwise fall back to a simple heuristic.

    Note: heuristic is not exact but useful to avoid very large prompts when tiktoken is not available.
    """
    try:
        import tiktoken

        # Try to pick the right encoding for known models; fallback to cl100k_base
        try:
            enc = tiktoken.encoding_for_model(model)
        except Exception:
            enc = tiktoken.get_encoding("cl100k_base")

        return len(enc.encode(text))
    except Exception:
        # Fallback heuristic: 1 token ~ 4 characters (approximation used in many tools)
        return max(1, int(len(text) / 4))
