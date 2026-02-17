"""
MongoDB Query Helpers - Common Aggregation Patterns

Centralizes MongoDB aggregation patterns and provides utilities for:
- Grouping and aggregation operations
- Date/time handling in aggregations
- Performance monitoring
- Query builders

All functions follow these patterns:
- Optional org_id parameter (defaults to DEFAULT_ORG_ID)
- Consistent error handling and logging
- Server-side aggregation (no Python-side filtering)
- Leverage MongoDB indexes
"""

import logging
from typing import List, Dict, Any, Optional
from datetime import datetime
from app.db.mongodb_config import get_database

logger = logging.getLogger(__name__)

DEFAULT_ORG_ID = "org_default"


def get_org_id(org_id: Optional[str] = None) -> str:
    """Get org_id, using default if not provided."""
    return org_id or DEFAULT_ORG_ID


# ============================================================================
# AGGREGATION PIPELINE BUILDERS
# ============================================================================

class AggregationBuilder:
    """Helper class for building MongoDB aggregation pipelines."""
    
    def __init__(self):
        self.stages = []
    
    def match(self, query: Dict[str, Any]) -> "AggregationBuilder":
        """Add $match stage."""
        self.stages.append({"$match": query})
        return self
    
    def group(self, id_expr: Any, fields: Dict[str, Any]) -> "AggregationBuilder":
        """Add $group stage."""
        group_doc = {"_id": id_expr}
        group_doc.update(fields)
        self.stages.append({"$group": group_doc})
        return self
    
    def sort(self, sort_spec: Dict[str, int]) -> "AggregationBuilder":
        """Add $sort stage."""
        self.stages.append({"$sort": sort_spec})
        return self
    
    def project(self, projection: Dict[str, Any]) -> "AggregationBuilder":
        """Add $project stage."""
        self.stages.append({"$project": projection})
        return self
    
    def limit(self, count: int) -> "AggregationBuilder":
        """Add $limit stage."""
        self.stages.append({"$limit": count})
        return self
    
    def build(self) -> List[Dict[str, Any]]:
        """Return the complete pipeline."""
        return self.stages


# ============================================================================
# COMMON AGGREGATION PATTERNS
# ============================================================================

def aggregate_group_by_date(
    collection_name: str,
    match_filter: Dict[str, Any],
    date_field: str = "period_start_date",
    sum_field: str = "sum_total",
    count_field: str = None,
    org_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Common pattern: Group by date and aggregate.
    
    Used for analytics: group care events by month, sum totals, count records.
    
    Args:
        collection_name: Name of collection to query
        match_filter: MongoDB $match filter (without org_id, will be added)
        date_field: Field name to group by (e.g., "period_start_date")
        sum_field: Field to sum (e.g., "sum_total")
        count_field: If provided, also count this field (typically None for count(*))
        org_id: Organization ID
        
    Returns:
        List of dicts with _id (date) and aggregated values
        
    Example:
        results = aggregate_group_by_date(
            "care_events",
            {"event_type": "SGBV"},
            sum_field="sum_total"
        )
    """
    org_id = get_org_id(org_id)
    db = get_database()
    
    # Combine organization filter with caller's filter
    match_doc = {"org_id": org_id}
    match_doc.update(match_filter)
    
    # Build group document
    group_doc = {
        "_id": f"${date_field}",
        "record_count": {"$sum": 1}
    }
    
    # Add sum if specified
    if sum_field:
        group_doc["total_amount"] = {"$sum": {"$toDouble": f"${sum_field}"}}
    
    pipeline = [
        {"$match": match_doc},
        {"$group": group_doc},
        {"$sort": {"_id": 1}}
    ]
    
    try:
        results = list(db[collection_name].aggregate(pipeline))
        logger.debug(f"Aggregation query on {collection_name}: {len(results)} results")
        return results
    except Exception as e:
        logger.error(f"Aggregation error on {collection_name}: {e}")
        raise


def aggregate_group_by_field(
    collection_name: str,
    match_filter: Dict[str, Any],
    group_field: str,
    sum_field: str = None,
    count_field: str = None,
    sort_by: str = None,
    org_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Group by any field and aggregate.
    
    Args:
        collection_name: Collection to query
        match_filter: $match filter
        group_field: Field to group by
        sum_field: Field to sum (optional)
        count_field: Field to count (optional, default is count records)
        sort_by: Field to sort results by (default sorts by _id)
        org_id: Organization ID
        
    Returns:
        List of aggregation results
    """
    org_id = get_org_id(org_id)
    db = get_database()
    
    # Build match
    match_doc = {"org_id": org_id}
    match_doc.update(match_filter)
    
    # Build group
    group_doc = {"_id": f"${group_field}"}
    
    if sum_field:
        group_doc["total_amount"] = {"$sum": {"$toDouble": f"${sum_field}"}}
    
    if count_field:
        group_doc["count"] = {"$sum": f"${count_field}"}
    else:
        group_doc["count"] = {"$sum": 1}
    
    # Build pipeline
    pipeline = [
        {"$match": match_doc},
        {"$group": group_doc}
    ]
    
    # Add sort
    if sort_by:
        if sort_by == "count_desc":
            pipeline.append({"$sort": {"count": -1}})
        elif sort_by == "total_desc":
            pipeline.append({"$sort": {"total_amount": -1}})
        else:
            pipeline.append({"$sort": {sort_by: 1}})
    else:
        pipeline.append({"$sort": {"_id": 1}})
    
    try:
        results = list(db[collection_name].aggregate(pipeline))
        logger.debug(f"Group aggregation on {collection_name}: {len(results)} results")
        return results
    except Exception as e:
        logger.error(f"Group aggregation error on {collection_name}: {e}")
        raise


def aggregate_multi_field_group(
    collection_name: str,
    match_filter: Dict[str, Any],
    group_fields: Dict[str, str],
    sum_fields: Dict[str, str] = None,
    org_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Group by multiple fields.
    
    Args:
        collection_name: Collection to query
        match_filter: $match filter
        group_fields: Dict of {field_name: field_alias} to group by
        sum_fields: Dict of {field_name: alias} fields to sum
        org_id: Organization ID
        
    Returns:
        List of aggregation results
        
    Example:
        results = aggregate_multi_field_group(
            "care_events",
            {"patient_id": patient_id},
            group_fields={"event_type": "type", "period_start_date": "date"},
            sum_fields={"sum_total": "amount"}
        )
    """
    org_id = get_org_id(org_id)
    db = get_database()
    
    # Build match
    match_doc = {"org_id": org_id}
    match_doc.update(match_filter)
    
    # Build group ID (compound)
    group_id = {}
    for field, alias in group_fields.items():
        group_id[alias] = f"${field}"
    
    # Build group document
    group_doc = {"_id": group_id, "record_count": {"$sum": 1}}
    
    if sum_fields:
        for field, alias in sum_fields.items():
            group_doc[alias] = {"$sum": {"$toDouble": f"${field}"}}
    
    pipeline = [
        {"$match": match_doc},
        {"$group": group_doc},
        {"$sort": {"_id": 1}}
    ]
    
    try:
        results = list(db[collection_name].aggregate(pipeline))
        logger.debug(f"Multi-field aggregation on {collection_name}: {len(results)} results")
        return results
    except Exception as e:
        logger.error(f"Multi-field aggregation error: {e}")
        raise


# ============================================================================
# CARE EVENTS SPECIFIC PATTERNS
# ============================================================================

def get_care_events_by_type_and_date(
    event_type: str,
    org_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Get care events grouped by date for a specific event type.
    
    Used by analytics endpoints for dashboard visualization.
    
    Args:
        event_type: Type of event (e.g., "SGBV", "SGBXI", "Verhinderungspflege")
        org_id: Organization ID
        
    Returns:
        List with _id (date), record_count, total_amount
    """
    return aggregate_group_by_date(
        collection_name="care_events",
        match_filter={"event_type": event_type},
        date_field="period_start_date",
        sum_field="sum_total",
        org_id=org_id
    )


def get_patient_care_summary(
    patient_id: str,
    org_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Get breakdown of care events by type for a patient.
    
    Used for patient histograms in chart generation.
    
    Args:
        patient_id: Patient ID
        org_id: Organization ID
        
    Returns:
        List with _id (event_type), count, total_amount
    """
    return aggregate_group_by_field(
        collection_name="care_events",
        match_filter={"patient_id": patient_id},
        group_field="event_type",
        sum_field="sum_total",
        sort_by="_id",
        org_id=org_id
    )


def get_patient_monthly_breakdown(
    patient_id: str,
    event_types: List[str] = None,
    org_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Get care events grouped by event_type AND date for a patient.
    
    Used for detailed patient histograms showing type breakdown by month.
    
    Args:
        patient_id: Patient ID
        event_types: Filter to specific event types (default: all)
        org_id: Organization ID
        
    Returns:
        List with _id.event_type, _id.date, record_count, total_amount
    """
    filter_doc = {"patient_id": patient_id}
    if event_types:
        filter_doc["event_type"] = {"$in": event_types}
    
    return aggregate_multi_field_group(
        collection_name="care_events",
        match_filter=filter_doc,
        group_fields={"event_type": "event_type", "period_start_date": "date"},
        sum_fields={"sum_total": "monthly_total"},
        org_id=org_id
    )


# ============================================================================
# BILLING SPECIFIC PATTERNS
# ============================================================================

def get_billing_summary_by_month(
    org_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Get billing summary grouped by month.
    
    Args:
        org_id: Organization ID
        
    Returns:
        List with _id (month), count (number of records), total_amount
    """
    return aggregate_group_by_field(
        collection_name="billing_details",
        match_filter={},
        group_field="billing_month",
        sum_field="total_amount",
        sort_by="_id",
        org_id=org_id
    )


def get_billing_by_event_type(
    org_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Get billing breakdown by event type.
    
    Args:
        org_id: Organization ID
        
    Returns:
        List with _id (event_type), count, total_amount (sorted descending by total)
    """
    return aggregate_group_by_field(
        collection_name="billing_details",
        match_filter={},
        group_field="event_type",
        sum_field="total_amount",
        sort_by="total_desc",
        org_id=org_id
    )


# ============================================================================
# RESULT FORMATTING
# ============================================================================

def format_date_grouped_results(
    results: List[Dict[str, Any]],
    date_field: str = "_id"
) -> List[Dict[str, Any]]:
    """
    Format aggregation results that were grouped by date.
    
    Takes MongoDB aggregation results with date strings and reformats for API response.
    
    Args:
        results: Aggregation results from get_care_events_by_type_and_date, etc.
        date_field: Name of the date field in results
        
    Returns:
        Formatted results with month/year breakdown
    """
    formatted = []
    
    for result in results:
        date_str = result.get(date_field, "")
        
        try:
            # Parse date from DD.MM.YY format
            dt = datetime.strptime(date_str.strip(), "%d.%m.%y")
            month_display = dt.strftime("%m/%Y")
            
            entry = {
                "month": month_display,
                "invoice_count": result.get("record_count", 0),
                "total_amount": round(float(result.get("total_amount", 0)), 2)
            }
            formatted.append(entry)
        except (ValueError, AttributeError) as e:
            logger.warning(f"Could not format date result {result}: {e}")
            continue
    
    return formatted


# ============================================================================
# PERFORMANCE MONITORING
# ============================================================================

import time
from functools import wraps


def monitor_query_performance(func):
    """Decorator to log query execution time."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        elapsed = time.time() - start_time
        
        logger.info(f"{func.__name__} took {elapsed:.3f}s")
        
        return result
    
    return wrapper
