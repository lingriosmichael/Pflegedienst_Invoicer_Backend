"""
Database package with MongoDB repository layer.

Usage:
    from app.db import (
        InvoiceRepository,
        PatientRepository,
        CareEventRepository,
        ServiceRepository,
        BillingRepository,
        BillingSummaryRepository
    )
"""

# MongoDB repositories (primary interface)
from app.db.mongodb_repositories import (
    InvoiceRepository,
    PatientRepository,
    CareEventRepository,
    ServiceRepository,
    BillingRepository,
    BillingSummaryRepository,
    DEFAULT_ORG_ID,
)

# Connection and configuration
from app.db.mongodb_config import (
    get_client,
    get_database,
    health_check,
)

from app.db.mongodb_schema import (
    create_collections_and_indexes,
    verify_schema,
)

__all__ = [
    # Repositories
    "InvoiceRepository",
    "PatientRepository",
    "CareEventRepository",
    "ServiceRepository",
    "BillingRepository",
    "BillingSummaryRepository",
    # Configuration
    "get_client",
    "get_database",
    "health_check",
    "create_collections_and_indexes",
    "verify_schema",
    "DEFAULT_ORG_ID",
]
