"""
Pydantic models for invoice data validation.
Provides type checking and validation at API boundaries.
"""

from pydantic import BaseModel, Field, validator, root_validator
from typing import List, Optional


class PatientSchema(BaseModel):
    """Validated patient data."""
    
    name: str = Field(..., min_length=1, max_length=200, description="Patient full name")
    birthdate: str = Field(..., regex=r'^\d{2}\.\d{2}\.(\d{2}|\d{4})$', description="DD.MM.YY or DD.MM.YYYY")
    insurance_number: str = Field(..., min_length=1, max_length=50, description="Insurance/health number")
    care_level: Optional[str] = Field(None, regex=r'^[0-5]$', description="Pflegegrad: 0-5")
    pflege_konto: Optional[str] = Field(None, description="Care account code")
    address: Optional[str] = Field(None, description="Patient address")
    debtor_number: Optional[str] = Field(None, description="Debtor/billing number")
    
    class Config:
        schema_extra = {
            "example": {
                "name": "Müller, Karl",
                "birthdate": "06.01.1941",
                "insurance_number": "L011897478",
                "care_level": "4",
                "pflege_konto": "4030"
            }
        }


class ServiceSchema(BaseModel):
    """Validated service/line item."""
    
    code: str = Field(..., min_length=1, description="Service code (e.g., '0101002a')")
    description: str = Field(..., min_length=1, description="Service description")
    quantity: str = Field(..., description="Number of services (German decimal format)")
    unit_price: str = Field(..., regex=r'^\d+[.,]\d{2}$', description="Price per unit (German format)")
    total_price: str = Field(..., regex=r'^\d+[.,]\d{2}$', description="Total cost (German format)")
    
    @validator('quantity')
    def validate_quantity(cls, v):
        """Ensure quantity can be parsed as decimal."""
        try:
            val = float(v.replace(',', '.'))
            if val < 0:
                raise ValueError("Quantity must be positive")
            return v
        except ValueError:
            raise ValueError(f"Invalid quantity: {v}")
    
    class Config:
        schema_extra = {
            "example": {
                "code": "0101002a",
                "description": "Kleine Morgen/Abendtoilette mit",
                "quantity": "31",
                "unit_price": "15,41",
                "total_price": "477,71"
            }
        }


class InvoiceSchema(BaseModel):
    """Validated invoice data."""
    
    pflegezeitraum_beginn: str = Field(..., regex=r'^\d{2}\.\d{2}\.\d{2}$')
    pflegezeitraum_ende: str = Field(..., regex=r'^\d{2}\.\d{2}\.\d{2}$')
    summe_covered: str = Field(..., regex=r'^\d+[.,]\d{2}$')
    summe_total: str = Field(..., regex=r'^\d+[.,]\d{2}$')
    care_account: Optional[str] = Field(None)
    abrechnungsmonat: Optional[str] = Field(None, regex=r'^\d{6}$')  # MMYYYY
    
    @root_validator
    def validate_amounts(cls, values):
        """Ensure total >= covered."""
        covered = float(values['summe_covered'].replace('.', '').replace(',', '.'))
        total = float(values['summe_total'].replace('.', '').replace(',', '.'))
        
        if total < covered:
            raise ValueError("summe_total must be >= summe_covered")
        
        return values


class StructuredInvoiceSchema(BaseModel):
    """Complete validated invoice structure."""
    
    patient: PatientSchema
    invoice: InvoiceSchema
    services: List[ServiceSchema]
    
    @validator('services', pre=True)
    def validate_services_not_empty(cls, v):
        if not v or len(v) == 0:
            raise ValueError("At least one service is required")
        return v
    
    class Config:
        schema_extra = {
            "example": {
                "patient": {
                    "name": "Müller, Karl",
                    "birthdate": "06.01.41",
                    "insurance_number": "L011897478",
                    "care_level": "4",
                    "pflege_konto": "4030"
                },
                "invoice": {
                    "pflegezeitraum_beginn": "01.03.25",
                    "pflegezeitraum_ende": "31.03.25",
                    "summe_covered": "1.758,61",
                    "summe_total": "1.858,61"
                },
                "services": [
                    {
                        "quantity": "31",
                        "code": "0101002a",
                        "description": "Kleine Morgen/Abendtoilette mit",
                        "unit_price": "15,41",
                        "total_price": "477,71"
                    }
                ]
            }
        }
