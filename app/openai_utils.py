import os
import json
import hashlib
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Simple file-based cache directory
CACHE_DIR = "cache/openai"
os.makedirs(CACHE_DIR, exist_ok=True)


def _cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, f"{key}.json")


def make_cache_key(model: str, system_prompt: str, user_text: str) -> str:
    """Create a stable sha256 cache key for the request."""
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    h.update(b"\n--SYSTEM--\n")
    h.update(system_prompt.encode("utf-8"))
    h.update(b"\n--USER--\n")
    h.update(user_text.encode("utf-8"))
    return h.hexdigest()


def get_cached_response(key: str) -> Optional[dict]:
    path = _cache_path(key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to read cache file {path}: {e}")
        return None


def set_cached_response(key: str, response_obj: dict):
    path = _cache_path(key)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(response_obj, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Failed to write cache file {path}: {e}")


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
