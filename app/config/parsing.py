"""
Parsing configuration for PDF processing.
Centralizes configurable thresholds and constants.
"""

import os


class ParsingConfig:
    """Parsing configuration with environment variable overrides."""

    # Minimum amount in EUR to include a service in invoice
    MIN_AMOUNT: float = float(os.getenv("PARSING_MIN_AMOUNT", "128.0"))

    # Quantity tolerance for matching services (as percentage, e.g., 0.5 = 50% tolerance)
    QUANTITY_TOLERANCE: float = float(os.getenv("PARSING_QUANTITY_TOLERANCE", "0.5"))

    # Maximum tokens per API call (conservative limit to stay under model limits)
    # gpt-4-mini: 128K token context, but be conservative with prompt
    MAX_TOKENS_PER_CALL: int = int(os.getenv("PARSING_MAX_TOKENS_PER_CALL", "6000"))

    # Default care period pattern
    CARE_PERIOD_FORMAT: str = "DD.MM.YYYY - DD.MM.YYYY"

    @classmethod
    def get_min_amount(cls) -> float:
        """Get minimum amount threshold (reloadable from env)."""
        return float(os.getenv("PARSING_MIN_AMOUNT", "128.0"))

    @classmethod
    def get_quantity_tolerance(cls) -> float:
        """Get quantity tolerance threshold (reloadable from env)."""
        return float(os.getenv("PARSING_QUANTITY_TOLERANCE", "0.5"))

    @classmethod
    def get_max_tokens_per_call(cls) -> int:
        """Get max tokens per API call (reloadable from env)."""
        return int(os.getenv("PARSING_MAX_TOKENS_PER_CALL", "6000"))

    @classmethod
    def get_config_summary(cls) -> dict:
        """Get current configuration as dictionary."""
        return {
            "min_amount": cls.get_min_amount(),
            "quantity_tolerance": cls.get_quantity_tolerance(),
            "max_tokens_per_call": cls.get_max_tokens_per_call(),
            "care_period_format": cls.CARE_PERIOD_FORMAT,
        }
