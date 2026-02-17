"""
Chart data generation using MongoDB aggregation queries.
Generates data for dashboards and visualizations.
"""
from typing import Dict, Any, List
from datetime import datetime
from app.core.logging import get_logger
from app.db import CareEventRepository, PatientRepository

logger = get_logger(__name__)


def generate_patient_histogram(patient_id: str) -> Dict[str, Any]:
    """
    Generate histogram of invoice amounts by month for a specific patient.
    Uses MongoDB aggregation to get SGBXI and Entleistung events grouped by type and month.
    
    Args:
        patient_id: The ID (string) of the patient to generate the histogram for
        
    Returns:
        Dictionary with data array ready for charting and title
        
    Example:
        result = generate_patient_histogram("pat_123")
        # Returns: {
        #     "data": [
        #         {"month": "01/2025", "SGBXI": 100.00, "Entleistung": 50.00},
        #         {"month": "02/2025", "SGBXI": 150.00, "Entleistung": 0.00}
        #     ],
        #     "title": "Invoice Amounts by Month"
        # }
    """
    try:
        # Use MongoDB aggregation to get data grouped by event_type and date
        aggregation_results = CareEventRepository.get_patient_histogram(
            patient_id=patient_id,
            event_types=["SGBXI", "Entleistung"]
        )
        
        if not aggregation_results:
            logger.warning(f"No SGBXI/Entleistung events found for patient {patient_id}")
            return {
                "data": [],
                "title": "No Data"
            }
        
        # Convert aggregation results to chart format
        month_data = {}
        years_present = set()
        
        for result in aggregation_results:
            id_info = result.get("_id", {})
            event_type = id_info.get("event_type", "")
            date_str = id_info.get("date", "")
            monthly_total = result.get("monthly_total", 0)
            
            if date_str:
                try:
                    # Parse date from DD.MM.YY format
                    date_str = date_str.strip()
                    dt = datetime.strptime(date_str, "%d.%m.%y")
                    month_key = dt.strftime("%m%Y")
                    month_display = dt.strftime("%m/%Y")
                    year = dt.strftime("%Y")
                    years_present.add(year)
                except (ValueError, TypeError) as e:
                    logger.warning(f"Could not parse date '{date_str}': {e}")
                    continue
                
                # Convert amount to float
                try:
                    amount = float(monthly_total) if monthly_total else 0.0
                except (ValueError, TypeError):
                    amount = 0.0
                
                # Initialize month if not exists
                if month_key not in month_data:
                    month_data[month_key] = {"display": month_display, "SGBXI": 0.0, "Entleistung": 0.0}
                
                # Add amount to the appropriate event type
                if event_type in ["SGBXI", "Entleistung"]:
                    month_data[month_key][event_type] += amount
        
        # Fill in all 12 months for each year present
        if years_present:
            for year in sorted(years_present):
                for month_num in range(1, 13):
                    month_key = f"{month_num:02d}{year}"
                    if month_key not in month_data:
                        month_display = f"{month_num:02d}/{year}"
                        month_data[month_key] = {"display": month_display, "SGBXI": 0.0, "Entleistung": 0.0}
        
        # Convert to sorted array format for charting
        chart_data = []
        for month_key in sorted(month_data.keys()):
            entry = {
                "month": month_data[month_key]["display"],
                "SGBXI": round(month_data[month_key]["SGBXI"], 2),
                "Entleistung": round(month_data[month_key]["Entleistung"], 2),
            }
            chart_data.append(entry)
        
        logger.info(f"Generated histogram for patient {patient_id} with {len(chart_data)} months of data")
        
        return {
            "data": chart_data,
            "title": f"Invoice Amounts by Month"
        }
        
    except Exception as e:
        logger.error(f"Error generating histogram for patient {patient_id}: {e}", exc_info=True)
        return {
            "data": [],
            "title": "Error generating chart"
        }


def get_all_patients() -> List[Dict]:
    """
    Get all patients in the database.
    
    Returns:
        List of dicts with id (patient_id) and name (patient_name)
    """
    try:
        patients = PatientRepository.find_all()
        
        result = []
        for patient in patients:
            result.append({
                "id": patient.get("patient_id"),
                "name": patient.get("patient_name")
            })
        
        logger.info(f"Fetched {len(result)} patients")
        return result
    except Exception as e:
        logger.error(f"Error fetching patients: {e}")
        return []