"""
Simple dashboard using direct SQL queries to get invoice data.
No AI agent needed - just restructure SQL results for charting.
"""
from typing import Dict, Any, List
from app.core.logging import get_logger
from app.db.connection import get_db

logger = get_logger(__name__)


def generate_patient_histogram(patient_id: int) -> Dict[str, Any]:
    """
    Generate histogram of invoice amounts by month for a specific patient.
    Direct SQL query grouped by care_range_begin month and care_account type.
    
    SGBXI: care_account in (4010, 4020)
    Entleistung: care_account = 4064
    
    Args:
        patient_id: The ID of the patient to generate the histogram for
        
    Returns:
        Dictionary with data array ready for charting
    """
    try:
        from datetime import datetime
        
        with get_db() as conn:
            c = conn.cursor()
            
            # Get all invoices for this patient
            c.execute("""
                SELECT care_range_begin, care_account, sum_total
                FROM invoices
                WHERE patient_id = ?
                ORDER BY care_range_begin
            """, (patient_id,))
            
            rows = c.fetchall()
            
            if not rows:
                logger.warning(f"No invoices found for patient {patient_id}")
                return {
                    "data": [],
                    "title": "No Data"
                }
            
            # Group by month and account type
            month_data = {}
            years_present = set()
            
            for row in rows:
                date_str, care_account, sum_total = row
                
                # Convert sum_total to float
                try:
                    amount = float(str(sum_total).replace(",", "."))
                except (ValueError, TypeError):
                    amount = 0.0
                
                # Parse date from DD.MM.YY format
                try:
                    dt = datetime.strptime(date_str, "%d.%m.%y")
                    month_key = dt.strftime("%m%Y")
                    month_display = dt.strftime("%m/%Y")
                    year = dt.strftime("%Y")
                    years_present.add(year)
                except (ValueError, TypeError):
                    logger.warning(f"Could not parse date: {date_str}")
                    continue
                
                # Classify by care_account
                if care_account in ("4010", "4020"):
                    account_type = "SGBXI"
                elif care_account == "4064":
                    account_type = "Entleistung"
                else:
                    account_type = "Other"
                
                if month_key not in month_data:
                    month_data[month_key] = {"display": month_display, "data": {}}
                
                if account_type not in month_data[month_key]["data"]:
                    month_data[month_key]["data"][account_type] = 0.0
                
                month_data[month_key]["data"][account_type] += amount
            
            # Fill in all 12 months for each year present
            if years_present:
                for year in sorted(years_present):
                    for month_num in range(1, 13):
                        month_key = f"{month_num:02d}{year}"
                        if month_key not in month_data:
                            month_display = f"{month_num:02d}/{year}"
                            month_data[month_key] = {"display": month_display, "data": {}}
            
            # Convert to array format for charting
            chart_data = []
            for month_key in sorted(month_data.keys()):
                entry = {
                    "month": month_data[month_key]["display"],
                    "SGBXI": round(month_data[month_key]["data"].get("SGBXI", 0), 2),
                    "Entleistung": round(month_data[month_key]["data"].get("Entleistung", 0), 2),
                }
                chart_data.append(entry)
            
            logger.info(f"Generated histogram for patient {patient_id} with {len(chart_data)} months")
            
            return {
                "data": chart_data,
                "title": f"Invoice Amounts by Month"
            }
            
    except Exception as e:
        logger.error(f"Error generating histogram for patient {patient_id}: {e}")
        return {
            "data": [],
            "title": "Error generating chart"
        }


def get_all_patients() -> List[Dict]:
    """
    Get all patients in the database using direct SQL query.
    
    Returns:
        List of dicts with id and name
    """
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT id, name FROM patients ORDER BY name")
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

