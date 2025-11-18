"""
Domain-specific exceptions for the invoicer application.
Provides clear error context for debugging and error handling.
"""


class InvoicerException(Exception):
    """Base exception for all invoicer-related errors."""
    pass


class ValidationError(InvoicerException):
    """Input validation failed (e.g., missing required field)."""
    
    def __init__(self, field: str, message: str):
        self.field = field
        super().__init__(f"Validation error in '{field}': {message}")


class ParseError(InvoicerException):
    """PDF or data parsing failed."""
    
    def __init__(self, chunk_id: int = None, reason: str = None):
        self.chunk_id = chunk_id
        if chunk_id and reason:
            super().__init__(f"Failed to parse chunk {chunk_id}: {reason}")
        else:
            super().__init__(f"Parsing failed: {reason or 'unknown error'}")


class InvoiceNotFoundError(InvoicerException):
    """Invoice lookup failed."""
    
    def __init__(self, invoice_id: int):
        self.invoice_id = invoice_id
        super().__init__(f"Invoice {invoice_id} not found")


class PatientNotFoundError(InvoicerException):
    """Patient lookup failed."""
    
    def __init__(self, insurance_number: str):
        self.insurance_number = insurance_number
        super().__init__(f"Patient with insurance number '{insurance_number}' not found")


class InsufficientDataError(InvoicerException):
    """Required fields are missing."""
    
    def __init__(self, entity: str, fields: list):
        self.entity = entity
        self.fields = fields
        super().__init__(f"Missing required fields in {entity}: {', '.join(fields)}")


class DatabaseError(InvoicerException):
    """Database operation failed."""
    
    def __init__(self, operation: str, message: str = None):
        self.operation = operation
        super().__init__(f"Database {operation} failed: {message or 'unknown error'}")


class ConfigurationError(InvoicerException):
    """Configuration is invalid or missing."""
    
    def __init__(self, setting: str, message: str = None):
        self.setting = setting
        super().__init__(f"Configuration error in '{setting}': {message or 'invalid value'}")
