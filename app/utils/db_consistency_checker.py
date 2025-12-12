"""
Database data consistency checker and fixer.
Scans the database for format inconsistencies and repairs them.
"""

import logging
from app.db.connection import get_db
from app.utils.data_validation import DataNormalizer, DataValidationError

logger = logging.getLogger(__name__)


class DatabaseConsistencyChecker:
    """Checks and fixes data format inconsistencies in the database."""
    
    @staticmethod
    def check_and_fix_all():
        """Run all consistency checks and fixes."""
        logger.info("="*80)
        logger.info("Starting database consistency check and repair...")
        logger.info("="*80)
        
        results = {
            "patients_birthdates": DatabaseConsistencyChecker.fix_patient_birthdates(),
            "invoices_amounts": DatabaseConsistencyChecker.fix_invoice_amounts(),
            "services_amounts": DatabaseConsistencyChecker.fix_service_amounts(),
            "care_services_amounts": DatabaseConsistencyChecker.fix_care_service_amounts(),
        }
        
        logger.info("="*80)
        logger.info("Database consistency check complete!")
        logger.info("="*80)
        for check_name, result in results.items():
            logger.info(f"  {check_name}: {result['fixed']} fixed, {result['failed']} failed")
        
        return results
    
    @staticmethod
    def fix_patient_birthdates():
        """
        Check and fix patient birthdates.
        Ensures all birthdates are in DD.MM.YYYY format with 4-digit years.
        """
        with get_db() as conn:
            c = conn.cursor()
            
            # Get all patients
            c.execute("SELECT id, name, birthdate FROM patients WHERE birthdate IS NOT NULL AND birthdate != ''")
            patients = c.fetchall()
            
            fixed = 0
            failed = 0
            
            logger.info(f"\nChecking {len(patients)} patient birthdates...")
            
            for patient_id, name, birthdate in patients:
                # Check if birthdate needs fixing (2-digit year)
                if birthdate and len(birthdate.split('.')[-1]) == 2:
                    try:
                        normalized = DataNormalizer.normalize_birthdate(birthdate)
                        c.execute("UPDATE patients SET birthdate = ? WHERE id = ?", (normalized, patient_id))
                        logger.info(f"  ✓ Patient {name} (ID {patient_id}): {birthdate} → {normalized}")
                        fixed += 1
                    except DataValidationError as e:
                        logger.warning(f"  ✗ Patient {name} (ID {patient_id}): Failed to fix birthdate '{birthdate}': {e}")
                        failed += 1
                elif birthdate and ',' in birthdate:
                    # Handle any other format issues
                    try:
                        normalized = DataNormalizer.normalize_birthdate(birthdate)
                        c.execute("UPDATE patients SET birthdate = ? WHERE id = ?", (normalized, patient_id))
                        logger.info(f"  ✓ Patient {name} (ID {patient_id}): {birthdate} → {normalized}")
                        fixed += 1
                    except DataValidationError as e:
                        logger.warning(f"  ✗ Patient {name} (ID {patient_id}): Failed to fix birthdate '{birthdate}': {e}")
                        failed += 1
            
            conn.commit()
            logger.info(f"Birthdates: {fixed} fixed, {failed} failed")
            
            return {"fixed": fixed, "failed": failed}
    
    @staticmethod
    def fix_invoice_amounts():
        """
        Check and fix invoice amounts.
        Ensures all amounts use "." as decimal separator (not ",").
        """
        with get_db() as conn:
            c = conn.cursor()
            
            # Get all invoices with amount fields
            c.execute("SELECT id, patient_id, sum_covered, sum_total, amount_owed FROM invoices")
            invoices = c.fetchall()
            
            fixed = 0
            failed = 0
            
            logger.info(f"\nChecking {len(invoices)} invoice amounts...")
            
            for invoice_id, patient_id, sum_covered, sum_total, amount_owed in invoices:
                updates = {}
                
                # Check sum_covered
                if sum_covered and ',' in str(sum_covered):
                    try:
                        normalized = DataNormalizer.normalize_amount(sum_covered)
                        updates['sum_covered'] = normalized
                        logger.info(f"  ✓ Invoice {invoice_id}: sum_covered {sum_covered} → {normalized}")
                    except DataValidationError as e:
                        logger.warning(f"  ✗ Invoice {invoice_id}: Failed to fix sum_covered '{sum_covered}': {e}")
                        failed += 1
                
                # Check sum_total
                if sum_total and ',' in str(sum_total):
                    try:
                        normalized = DataNormalizer.normalize_amount(sum_total)
                        updates['sum_total'] = normalized
                        logger.info(f"  ✓ Invoice {invoice_id}: sum_total {sum_total} → {normalized}")
                    except DataValidationError as e:
                        logger.warning(f"  ✗ Invoice {invoice_id}: Failed to fix sum_total '{sum_total}': {e}")
                        failed += 1
                
                # Check amount_owed
                if amount_owed and ',' in str(amount_owed):
                    try:
                        normalized = DataNormalizer.normalize_amount(amount_owed)
                        updates['amount_owed'] = normalized
                        logger.info(f"  ✓ Invoice {invoice_id}: amount_owed {amount_owed} → {normalized}")
                    except DataValidationError as e:
                        logger.warning(f"  ✗ Invoice {invoice_id}: Failed to fix amount_owed '{amount_owed}': {e}")
                        failed += 1
                
                # Apply updates
                if updates:
                    if 'sum_covered' in updates:
                        c.execute("UPDATE invoices SET sum_covered = ? WHERE id = ?", (updates['sum_covered'], invoice_id))
                    if 'sum_total' in updates:
                        c.execute("UPDATE invoices SET sum_total = ? WHERE id = ?", (updates['sum_total'], invoice_id))
                    if 'amount_owed' in updates:
                        c.execute("UPDATE invoices SET amount_owed = ? WHERE id = ?", (updates['amount_owed'], invoice_id))
                    fixed += len(updates)
            
            conn.commit()
            logger.info(f"Invoice amounts: {fixed} fixed, {failed} failed")
            
            return {"fixed": fixed, "failed": failed}
    
    @staticmethod
    def fix_service_amounts():
        """
        Check and fix service amounts and quantities.
        Ensures all amounts use "." as decimal separator.
        """
        with get_db() as conn:
            c = conn.cursor()
            
            # Get all services with amount fields
            c.execute("SELECT id, invoice_id, quantity, unit_price, total_price FROM services")
            services = c.fetchall()
            
            fixed = 0
            failed = 0
            
            logger.info(f"\nChecking {len(services)} service amounts...")
            
            for service_id, invoice_id, quantity, unit_price, total_price in services:
                updates = {}
                
                # Check quantity
                if quantity and ',' in str(quantity):
                    normalized = str(quantity).replace(',', '.')
                    updates['quantity'] = normalized
                    logger.info(f"  ✓ Service {service_id}: quantity {quantity} → {normalized}")
                
                # Check unit_price
                if unit_price and ',' in str(unit_price):
                    try:
                        normalized = DataNormalizer.normalize_amount(unit_price)
                        updates['unit_price'] = normalized
                        logger.info(f"  ✓ Service {service_id}: unit_price {unit_price} → {normalized}")
                    except DataValidationError as e:
                        logger.warning(f"  ✗ Service {service_id}: Failed to fix unit_price '{unit_price}': {e}")
                        failed += 1
                
                # Check total_price
                if total_price and ',' in str(total_price):
                    try:
                        normalized = DataNormalizer.normalize_amount(total_price)
                        updates['total_price'] = normalized
                        logger.info(f"  ✓ Service {service_id}: total_price {total_price} → {normalized}")
                    except DataValidationError as e:
                        logger.warning(f"  ✗ Service {service_id}: Failed to fix total_price '{total_price}': {e}")
                        failed += 1
                
                # Apply updates
                if updates:
                    if 'quantity' in updates:
                        c.execute("UPDATE services SET quantity = ? WHERE id = ?", (updates['quantity'], service_id))
                    if 'unit_price' in updates:
                        c.execute("UPDATE services SET unit_price = ? WHERE id = ?", (updates['unit_price'], service_id))
                    if 'total_price' in updates:
                        c.execute("UPDATE services SET total_price = ? WHERE id = ?", (updates['total_price'], service_id))
                    fixed += len(updates)
            
            conn.commit()
            logger.info(f"Service amounts: {fixed} fixed, {failed} failed")
            
            return {"fixed": fixed, "failed": failed}
    
    @staticmethod
    def fix_care_service_amounts():
        """
        Check and fix care service amounts and quantities.
        Ensures all amounts use "." as decimal separator.
        """
        with get_db() as conn:
            c = conn.cursor()
            
            # Get all care services with amount fields
            c.execute("SELECT id, care_record_id, quantity, unit_price, total_price FROM care_services")
            care_services = c.fetchall()
            
            fixed = 0
            failed = 0
            
            logger.info(f"\nChecking {len(care_services)} care service amounts...")
            
            for service_id, record_id, quantity, unit_price, total_price in care_services:
                updates = {}
                
                # Check quantity
                if quantity and ',' in str(quantity):
                    normalized = str(quantity).replace(',', '.')
                    updates['quantity'] = normalized
                    logger.info(f"  ✓ Care Service {service_id}: quantity {quantity} → {normalized}")
                
                # Check unit_price
                if unit_price and ',' in str(unit_price):
                    try:
                        normalized = DataNormalizer.normalize_amount(unit_price)
                        updates['unit_price'] = normalized
                        logger.info(f"  ✓ Care Service {service_id}: unit_price {unit_price} → {normalized}")
                    except DataValidationError as e:
                        logger.warning(f"  ✗ Care Service {service_id}: Failed to fix unit_price '{unit_price}': {e}")
                        failed += 1
                
                # Check total_price
                if total_price and ',' in str(total_price):
                    try:
                        normalized = DataNormalizer.normalize_amount(total_price)
                        updates['total_price'] = normalized
                        logger.info(f"  ✓ Care Service {service_id}: total_price {total_price} → {normalized}")
                    except DataValidationError as e:
                        logger.warning(f"  ✗ Care Service {service_id}: Failed to fix total_price '{total_price}': {e}")
                        failed += 1
                
                # Apply updates
                if updates:
                    if 'quantity' in updates:
                        c.execute("UPDATE care_services SET quantity = ? WHERE id = ?", (updates['quantity'], service_id))
                    if 'unit_price' in updates:
                        c.execute("UPDATE care_services SET unit_price = ? WHERE id = ?", (updates['unit_price'], service_id))
                    if 'total_price' in updates:
                        c.execute("UPDATE care_services SET total_price = ? WHERE id = ?", (updates['total_price'], service_id))
                    fixed += len(updates)
            
            conn.commit()
            logger.info(f"Care service amounts: {fixed} fixed, {failed} failed")
            
            return {"fixed": fixed, "failed": failed}
    
    @staticmethod
    def generate_report():
        """Generate a detailed report of current database state."""
        with get_db() as conn:
            c = conn.cursor()
            
            logger.info("\n" + "="*80)
            logger.info("DATABASE CONSISTENCY REPORT")
            logger.info("="*80)
            
            # Patient birthdates with 2-digit years
            logger.info("\nPatients with 2-digit year birthdates (need fixing):")
            c.execute("""
                SELECT id, name, birthdate 
                FROM patients 
                WHERE birthdate IS NOT NULL 
                  AND LENGTH(SUBSTR(birthdate, -2)) = 2
                LIMIT 20
            """)
            rows = c.fetchall()
            if rows:
                for patient_id, name, birthdate in rows:
                    logger.info(f"  - {name} (ID {patient_id}): {birthdate}")
            else:
                logger.info("  ✓ None found")
            
            # Invoice amounts with comma decimal
            logger.info("\nInvoices with comma decimal separators (need fixing):")
            c.execute("""
                SELECT id, sum_covered, sum_total, amount_owed 
                FROM invoices 
                WHERE sum_covered LIKE '%,%' 
                   OR sum_total LIKE '%,%' 
                   OR amount_owed LIKE '%,%'
                LIMIT 20
            """)
            rows = c.fetchall()
            if rows:
                for inv_id, covered, total, owed in rows:
                    logger.info(f"  - Invoice {inv_id}: covered={covered}, total={total}, owed={owed}")
            else:
                logger.info("  ✓ None found")
            
            # Service amounts with comma decimal
            logger.info("\nServices with comma decimal separators (need fixing):")
            c.execute("""
                SELECT id, unit_price, total_price 
                FROM services 
                WHERE unit_price LIKE '%,%' 
                   OR total_price LIKE '%,%'
                LIMIT 20
            """)
            rows = c.fetchall()
            if rows:
                for svc_id, unit_price, total_price in rows:
                    logger.info(f"  - Service {svc_id}: unit_price={unit_price}, total_price={total_price}")
            else:
                logger.info("  ✓ None found")
            
            # Summary statistics
            logger.info("\nDatabase Statistics:")
            c.execute("SELECT COUNT(*) FROM patients")
            logger.info(f"  Total patients: {c.fetchone()[0]}")
            
            c.execute("SELECT COUNT(*) FROM invoices")
            logger.info(f"  Total invoices: {c.fetchone()[0]}")
            
            c.execute("SELECT COUNT(*) FROM services")
            logger.info(f"  Total services: {c.fetchone()[0]}")
            
            c.execute("SELECT COUNT(*) FROM care_records")
            logger.info(f"  Total care records: {c.fetchone()[0]}")
            
            c.execute("SELECT COUNT(*) FROM care_services")
            logger.info(f"  Total care services: {c.fetchone()[0]}")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, '/Users/michaelfernandolingrios/Documents/Invoicer/pflegedienst_invoicer')
    
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Generate report first
    DatabaseConsistencyChecker.generate_report()
    
    # Then run fixes
    DatabaseConsistencyChecker.check_and_fix_all()
