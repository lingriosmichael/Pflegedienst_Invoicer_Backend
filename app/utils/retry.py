"""
Retry decorator with exponential backoff.
Handles transient failures (API rate limits, temporary network issues, etc.).
"""

import time
import logging
from functools import wraps
from typing import Callable, Any, Type, Tuple

logger = logging.getLogger(__name__)


def retry_with_backoff(
    max_retries: int = 3,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    max_delay: float = 60.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,)
):
    """
    Decorator to retry a function with exponential backoff.
    
    Args:
        max_retries: Maximum number of retry attempts (default: 3)
        initial_delay: Starting delay in seconds (default: 1.0)
        backoff_factor: Multiplier for delay after each retry (default: 2.0)
        max_delay: Maximum delay between retries (default: 60.0)
        exceptions: Tuple of exceptions to catch and retry on
    
    Returns:
        Decorated function that retries on specified exceptions
    
    Example:
        @retry_with_backoff(max_retries=3, initial_delay=2.0)
        def flaky_api_call():
            # This will retry up to 3 times if it raises Exception
            return requests.get("...")
    """
    
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            delay = initial_delay
            last_exception = None
            
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                
                except exceptions as e:
                    last_exception = e
                    
                    if attempt < max_retries:
                        # Calculate backoff with cap
                        actual_delay = min(delay, max_delay)
                        
                        logger.warning(
                            f"{func.__name__} failed (attempt {attempt + 1}/{max_retries + 1}). "
                            f"Retrying in {actual_delay:.1f}s... Error: {type(e).__name__}: {e}"
                        )
                        
                        time.sleep(actual_delay)
                        delay *= backoff_factor
                    else:
                        logger.error(
                            f"{func.__name__} failed after {max_retries + 1} attempts. "
                            f"Last error: {type(e).__name__}: {e}"
                        )
            
            # If we get here, all retries failed
            raise last_exception
        
        return wrapper
    
    return decorator
