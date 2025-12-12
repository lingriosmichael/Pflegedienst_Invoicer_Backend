"""
Data validation and normalization utilities.
Ensures consistent data format across imports:
- All amounts use "." as decimal separator (not ",")
- Birthdates use 4-digit year format (DDMM.YYYY)
- Validates required fields and data types
"""

import re
import logging
from typing import Dict, Any, Tuple, Optional
from app.utils.parsing import GermanDecimalParser

logger = logging.getLogger(__name__)


class DataValidationError(Exception):
    """Raised when data validation fails."""
    pass


class DataNormalizer:
    """Normalizes and validates invoice and patient data."""
    
    @staticmethod
    def normalize_amount(amount: Any) -> str:
        """
        Normalize an amount value to standard format (using "." as decimal separator).
        
        Args:
            amount: Value that might be in German or standard format
            
        Returns:
            str: Normalized amount with "." as decimal separator
            
        Raises:
            DataValidationError: If amount cannot be parsed
        """
        if amount is None or amount == "":
            raise DataValidationError("Amount is empty or None")
        
        try:
            # Use GermanDecimalParser to handle both German and standard formats
            float_val = GermanDecimalParser.parse(amount)
            # Return as string with "." decimal separator
            return GermanDecimalParser.to_float_string(float_val, decimal_places=2)
        except Exception as e:
            raise DataValidationError(f"Cannot parse amount '{amount}': {e}")
    
    @staticmethod
    def normalize_birthdate(birthdate: Any) -> str:
        """
        Normalize birthdate to DD.MM.YYYY format with 4-digit year.
        
        Args:
            birthdate: Birthdate in various formats (DD.MM.YY, DD.MM.YYYY, DD/MM/YY, etc.)
            
        Returns:
            str: Normalized birthdate in DD.MM.YYYY format
            
        Raises:
            DataValidationError: If birthdate cannot be parsed
        """
        if birthdate is None or birthdate == "":
            raise DataValidationError("Birthdate is empty or None")
        
        birthdate_str = str(birthdate).strip()
        
        # Try various date formats
        date_patterns = [
            (r'^(\d{2})[./\-](\d{2})[./\-](\d{4})$', lambda m: (m.group(1), m.group(2), m.group(3))),  # DD.MM.YYYY
            (r'^(\d{2})[./\-](\d{2})[./\-](\d{2})$', lambda m: (m.group(1), m.group(2), _expand_year(m.group(3)))),  # DD.MM.YY
        ]
        
        for pattern, extractor in date_patterns:
            match = re.match(pattern, birthdate_str)
            if match:
                day, month, year = extractor(match)
                
                # Validate day and month
                try:
                    day_int = int(day)
                    month_int = int(month)
                    year_int = int(year)
                    
                    if not (1 <= day_int <= 31):
                        raise DataValidationError(f"Invalid day: {day}")
                    if not (1 <= month_int <= 12):
                        raise DataValidationError(f"Invalid month: {month}")
                    if not (1900 <= year_int <= 2100):
                        raise DataValidationError(f"Invalid year: {year}")
                    
                    return f"{day}.{month}.{year}"
                except ValueError as e:
                    raise DataValidationError(f"Cannot parse birthdate components: {e}")
        
        raise DataValidationError(f"Birthdate '{birthdate_str}' does not match expected format (DD.MM.YY or DD.MM.YYYY)")
    
    @staticmethod
    def validate_invoice_record(record: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Validate an invoice record for required and properly formatted fields.
        
        Args:
            record: Invoice record dictionary
            
        Returns:
            Tuple: (is_valid: bool, error_message: Optional[str])
        """
        required_fields = [
            'patient_id', 'care_range_begin', 'care_range_end',
            'sum_covered', 'sum_total', 'care_account', 'invoicing_month'
        ]
        
        for field in required_fields:
            if field not in record or record[field] is None or record[field] == "":
                return False, f"Missing required field: {field}"
        
        # Validate invoicing_month format (MMYYYY)
        month_str = str(record['invoicing_month']).strip()
        if not re.match(r'^\d{6}$', month_str):
            return False, f"invoicing_month must be MMYYYY format, got: {month_str}"
        
        # Validate care_account format
        care_account = str(record['care_account']).strip()
        if not re.match(r'^\d{4}$', care_account):
            return False, f"care_account must be 4 digits, got: {care_account}"
        
        return True, None
    
    @staticmethod
    def validate_patient_record(record: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Validate a patient record for required and properly formatted fields.
        
        Args:
            record: Patient record dictionary
            
        Returns:
            Tuple: (is_valid: bool, error_message: Optional[str])
        """
        required_fields = ['name', 'birthdate', 'insurance_number', 'care_level', 'address']
        
        for field in required_fields:
            if field not in record or record[field] is None or record[field] == "":
                return False, f"Missing required field: {field}"
        
        # Validate birthdate can be parsed
        try:
            DataNormalizer.normalize_birthdate(record['birthdate'])
        except DataValidationError as e:
            return False, str(e)
        
        return True, None
    
    @staticmethod
    def normalize_invoice_data(data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize an invoice record, converting all amounts to standard format.
        
        Args:
            data: Raw invoice data
            
        Returns:
            Dict: Normalized invoice data
            
        Raises:
            DataValidationError: If normalization fails on required fields
        """
        normalized = data.copy()
        
        # Normalize amount fields
        amount_fields = ['sum_covered', 'sum_total', 'amount_owed']
        for field in amount_fields:
            if field in normalized and normalized[field] is not None and normalized[field] != "":
                try:
                    normalized[field] = DataNormalizer.normalize_amount(normalized[field])
                except DataValidationError as e:
                    # For optional fields, log warning but continue
                    if field == 'amount_owed':
                        logger.warning(f"Could not normalize {field}: {e}")
                    else:
                        raise  # Re-raise for required fields
        
        return normalized
    
    @staticmethod
    def normalize_patient_data(data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize a patient record, standardizing birthdate format.
        
        Args:
            data: Raw patient data
            
        Returns:
            Dict: Normalized patient data
            
        Raises:
            DataValidationError: If normalization fails on required fields
        """
        normalized = data.copy()
        
        # Normalize birthdate
        if 'birthdate' in normalized:
            try:
                normalized['birthdate'] = DataNormalizer.normalize_birthdate(normalized['birthdate'])
            except DataValidationError:
                raise
        
        return normalized
    
    @staticmethod
    def normalize_service_data(data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize a service record, converting all amounts to standard format.
        
        Args:
            data: Raw service data
            
        Returns:
            Dict: Normalized service data
        """
        normalized = data.copy()
        
        # Normalize amount fields
        amount_fields = ['unit_price', 'total_price']
        for field in amount_fields:
            if field in normalized and normalized[field] is not None and normalized[field] != "":
                try:
                    normalized[field] = DataNormalizer.normalize_amount(normalized[field])
                except DataValidationError as e:
                    logger.warning(f"Could not normalize service {field}: {e}")
        
        # Normalize quantity (also might have comma as decimal separator)
        if 'quantity' in normalized and normalized['quantity']:
            quantity_str = str(normalized['quantity']).strip()
            # Replace comma with period if it looks like a decimal
            if ',' in quantity_str:
                quantity_str = quantity_str.replace(',', '.')
            normalized['quantity'] = quantity_str
        
        return normalized


def _expand_year(year_str: str) -> str:
    """
    Expand 2-digit year to 4-digit year.
    
    Args:
        year_str: 2-digit year string (00-99)
        
    Returns:
        str: 4-digit year string
    """
    year_int = int(year_str)
    # Years 00-30 -> 2000-2030 (assume contemporary births)
    # Years 31-99 -> 1931-1999 (assume older births)
    if year_int <= 30:
        return f"20{year_int:02d}"
    else:
        return f"19{year_int:02d}"
