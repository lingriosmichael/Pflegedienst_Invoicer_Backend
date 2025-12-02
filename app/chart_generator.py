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
    Direct SQL query grouped by month and care_account type.
    
    SGBXI: care_account in (4010, 4020)
    Entleisung: care_account = 4064
    
    Args:
        patient_id: The ID of the patient to generate the histogram for
        
    Returns:
        Dictionary with data array ready for charting
    """
    try:
        with get_db() as conn:
            c = conn.cursor()
            
            # Get all invoices for this patient
            c.execute("""
                SELECT invoicing_month, care_account, sum_total
                FROM invoices
                WHERE patient_id = ?
                ORDER BY invoicing_month
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
            
            for row in rows:
                month, care_account, sum_total = row
                
                # Convert sum_total to float
                try:
                    amount = float(str(sum_total).replace(",", "."))
                except (ValueError, TypeError):
                    amount = 0.0
                
                # Classify by care_account
                if care_account in ("4010", "4020"):
                    account_type = "SGBXI"
                elif care_account == "4064":
                    account_type = "Entleisung"
                else:
                    account_type = "Other"
                
                if month not in month_data:
                    month_data[month] = {}
                
                if account_type not in month_data[month]:
                    month_data[month][account_type] = 0.0
                
                month_data[month][account_type] += amount
            
            # Convert to array format for charting
            chart_data = []
            
            # Determine the year from the data (use first month's year)
            if month_data:
                first_month = list(month_data.keys())[0]
                year = first_month[2:6] if len(first_month) >= 6 else "2025"
            else:
                year = "2025"
            
            # Create entries for all 12 months
            for month_num in range(1, 13):
                month_str = f"{month_num:02d}{year}"
                formatted_month = f"{month_num:02d}/{year}"
                
                entry = {
                    "month": formatted_month,
                    "SGBXI": round(month_data.get(month_str, {}).get("SGBXI", 0), 2),
                    "Entleisung": round(month_data.get(month_str, {}).get("Entleisung", 0), 2),
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

