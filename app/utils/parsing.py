"""
German number parsing utilities.
Handles conversion between German decimal format (1.234,56) and float/string representations.
"""

import logging
import math
from typing import Union

logger = logging.getLogger(__name__)


class GermanDecimalParser:
    """
    Utility class for parsing and formatting German decimal numbers.
    
    German format: 1.234,56 (periods for thousands, comma for decimal)
    Standard float: 1234.56 (no thousands separator, period for decimal)
    """
    
    @staticmethod
    def parse(val: Union[str, int, float]) -> float:
        """
        Parse a value that might be in German decimal format.
        
        Args:
            val: Value to parse (string, int, or float)
        
        Returns:
            float: Parsed value
        
        Examples:
            >>> GermanDecimalParser.parse("1.234,56")
            1234.56
            >>> GermanDecimalParser.parse("9,54")
            9.54
            >>> GermanDecimalParser.parse(123.45)
            123.45
        """
        if isinstance(val, (int, float)):
            result = float(val)
            if not math.isfinite(result) or result < 0:
                raise ValueError("Amount must be finite and nonnegative")
            return result
        
        if not isinstance(val, str):
            raise ValueError("Amount must be a number or monetary string")
        
        val = val.strip()
        
        if not val:
            raise ValueError("Amount is required")

        if '.' in val and ',' in val:
            # German format: "1.234,56" -> "1234.56"
            val = val.replace('.', '').replace(',', '.')
        elif ',' in val:
            # German format without thousand separator: "9,54" -> "9.54"
            val = val.replace(',', '.')
        # else assume it's already a valid float string: "190.8"

        try:
            return float(val)
        except ValueError:
            raise ValueError("Invalid monetary amount") from None

    @staticmethod
    def to_german_string(val: Union[str, int, float], decimal_places: int = 2) -> str:
        """
        Convert a value to German decimal format string.
        
        Args:
            val: Value to convert
            decimal_places: Number of decimal places (default: 2)
        
        Returns:
            str: Formatted string in German decimal format (e.g., "1.234,56")
        
        Examples:
            >>> GermanDecimalParser.to_german_string(1234.56)
            '1.234,56'
            >>> GermanDecimalParser.to_german_string("9.54")
            '9,54'
        """
        # First parse to float to normalize
        float_val = GermanDecimalParser.parse(val)

        # Format in US style with thousands separator comma and decimal point
        formatted = f"{float_val:,.{decimal_places}f}"

        # Convert US format (1,234.56) to German format (1.234,56)
        # Approach: swap separators safely using a temporary marker.
        tmp = formatted.replace(',', 'X')  # thousands separator -> X
        tmp = tmp.replace('.', ',')        # decimal point -> comma
        german = tmp.replace('X', '.')     # X -> period as thousands separator
        return german

    @staticmethod
    def to_float_string(val: Union[str, int, float], decimal_places: int = 2) -> str:
        """
        Convert a value to standard float string format.
        
        Args:
            val: Value to convert
            decimal_places: Number of decimal places (default: 2)
        
        Returns:
            str: Formatted string in float format (e.g., "1234.56")
        
        Examples:
            >>> GermanDecimalParser.to_float_string("1.234,56")
            '1234.56'
        """
        float_val = GermanDecimalParser.parse(val)
        return f"{float_val:.{decimal_places}f}"


def generate_id(prefix: str) -> str:
    """Generate unique ID with prefix for new unified schema."""
    import uuid
    return f"{prefix}_{uuid.uuid4().hex[:12]}"
