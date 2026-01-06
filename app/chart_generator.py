"""
Simple dashboard using direct SQL queries to get invoice data.
No AI agent needed - just restructure SQL results for charting.
"""
from typing import Dict, Any, List
from app.core.logging import get_logger
from app.db.connection import get_db

logger = get_logger(__name__)


def generate_patient_histogram(patient_id: str) -> Dict[str, Any]:
    """
    Generate histogram of invoice amounts by month for a specific patient from unified schema.
    Queries SGBXI and Entleistung events grouped by month (from period_start_date).
    
    Args:
        patient_id: The ID (string) of the patient to generate the histogram for
        
    Returns:
        Dictionary with data array ready for charting
    """
    try:
        from datetime import datetime
        
        with get_db() as conn:
            c = conn.cursor()
            
            # Get monthly totals for SGBXI and Entleistung separately
            c.execute("""
                SELECT 
                  event_type,
                  period_start_date,
                  SUM(sum_total) as monthly_total
                FROM care_events
                WHERE patient_id = ? AND event_type IN ('SGBXI', 'Entleistung')
                GROUP BY event_type, period_start_date
                ORDER BY period_start_date
            """, (patient_id,))
            
            rows = c.fetchall()
            
            if not rows:
                logger.warning(f"No SGBXI/Entleistung events found for patient {patient_id}")
                return {
                    "data": [],
                    "title": "No Data"
                }
            
            # Group by month and event type
            month_data = {}
            years_present = set()
            
            for event_type, date_str, monthly_total in rows:
                # Parse date from DD.MM.YY format
                try:
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
    Get all patients in the database from unified schema.
    
    Returns:
        List of dicts with id (patient_id) and name (patient_name)
    """
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT patient_id, patient_name FROM patient_profiles ORDER BY patient_name")
            rows = c.fetchall()
            
            result = []
            for row in rows:
                patient_id, patient_name = row
                result.append({
                    "id": patient_id,
                    "name": patient_name
                })
            
            return result
    except Exception as e:
        logger.error(f"Error fetching patients: {e}")
        return []

