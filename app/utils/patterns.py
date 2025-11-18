"""
Regex pattern registry for invoice parsing.
Centralizes all regex patterns to reduce duplication and improve maintainability.
"""

import re
from enum import Enum


class InvoicePatterns(Enum):
    """
    Pre-compiled regex patterns for invoice parsing.
    Using Enum provides a type-safe registry and enables caching.
    """
    
    # Care period pattern: "Pflegezeitraum: 01.03.25 - 31.03.25"
    CARE_PERIOD = re.compile(
        r"Pflegezeitraum:\s*(\d{2}\.\d{2}\.\d{2})\s*-\s*(\d{2}\.\d{2}\.\d{2})",
        re.IGNORECASE
    )
    
    # Euro amount pattern: "1.234,56" or "1234,56"
    EURO_AMOUNT = re.compile(
        r"^[\d\.]+,\d{2}$"
    )
    
    # Care account pattern: "Pflegekonto: 4064"
    CARE_ACCOUNT = re.compile(
        r"Pflegekonto:\s*(\d{4})",
        re.IGNORECASE
    )
    
    # Insurance number pattern (typical German format)
    INSURANCE_NUMBER = re.compile(
        r"[A-Za-z]{1}\d{10,}"
    )
    
    # Birthdate pattern: "06.01.1941" or "06.01.41"
    BIRTHDATE = re.compile(
        r"(\d{2})\.(\d{2})\.(\d{2,4})"
    )
    
    # Service code pattern: "0101002a" or similar
    SERVICE_CODE = re.compile(
        r"^\d{6,8}[a-z]?$",
        re.IGNORECASE
    )
    
    # Quantity pattern: "31" or "31,5" (German format)
    QUANTITY = re.compile(
        r"^\d+([.,]\d+)?$"
    )
    
    # Verordnung section start
    VERORDNUNG_START = re.compile(
        r"^Verordnung:",
        re.IGNORECASE
    )
    
    # Summe Euro line: "Summe €"
    SUMME_LINE = re.compile(
        r"^Summe\s*€",
        re.IGNORECASE
    )
    
    # Patient name pattern (usually "Last, First")
    PATIENT_NAME = re.compile(
        r"([A-Za-zÄäÖöÜüß\-]+),\s*([A-Za-zÄäÖöÜüß\s\-]+)"
    )

    @staticmethod
    def get_pattern(pattern_enum: 'InvoicePatterns') -> re.Pattern:
        """
        Get the compiled regex pattern.
        
        Args:
            pattern_enum: InvoicePatterns enum value
        
        Returns:
            re.Pattern: The compiled regex pattern
        
        Example:
            pattern = InvoicePatterns.get_pattern(InvoicePatterns.CARE_PERIOD)
            match = pattern.search(text)
        """
        return pattern_enum.value


# Export patterns for convenience
CARE_PERIOD_PATTERN = InvoicePatterns.CARE_PERIOD.value
EURO_AMOUNT_PATTERN = InvoicePatterns.EURO_AMOUNT.value
CARE_ACCOUNT_PATTERN = InvoicePatterns.CARE_ACCOUNT.value
INSURANCE_NUMBER_PATTERN = InvoicePatterns.INSURANCE_NUMBER.value
BIRTHDATE_PATTERN = InvoicePatterns.BIRTHDATE.value
SERVICE_CODE_PATTERN = InvoicePatterns.SERVICE_CODE.value
QUANTITY_PATTERN = InvoicePatterns.QUANTITY.value
VERORDNUNG_START_PATTERN = InvoicePatterns.VERORDNUNG_START.value
SUMME_LINE_PATTERN = InvoicePatterns.SUMME_LINE.value
PATIENT_NAME_PATTERN = InvoicePatterns.PATIENT_NAME.value
