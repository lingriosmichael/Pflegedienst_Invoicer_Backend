"""
Repository classes for database access.
Implements atomic invoice number generation and centralizes all SQL queries.

Pattern: All database operations go through repositories.
Benefit: Single point for query optimization, testing, and error handling.
"""

import logging
from datetime import datetime
from app.db.connection import get_db
from app.exceptions import InvoiceNotFoundError, PatientNotFoundError

logger = logging.getLogger(__name__)

class InvoiceRepository:
    """Manages all invoice-related database operations."""
    
    @staticmethod
    def get_next_invoice_number():
        """
        Atomically get the next invoice number.
        Uses database locking to prevent duplicate numbers under concurrent load.
        """
        with get_db() as conn:
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS invoice_sequences (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_number INTEGER NOT NULL DEFAULT 0
                )
            """)
            
            # Ensure exactly one row exists
            c.execute("SELECT COUNT(*) FROM invoice_sequences")
            if c.fetchone()[0] == 0:
                c.execute("INSERT INTO invoice_sequences (id, last_number) VALUES (1, 0)")
            
            # Atomic increment
            c.execute("UPDATE invoice_sequences SET last_number = last_number + 1 WHERE id = 1")
            c.execute("SELECT last_number FROM invoice_sequences WHERE id = 1")
            next_number = c.fetchone()[0]
            conn.commit()
            
            logger.info(f"Generated invoice number: {next_number}")
            return next_number

    @staticmethod
    def find_by_month(month: str, private_only: bool = False):
        """Get all invoice IDs for a given month, optionally filtering for private invoices."""
        with get_db() as conn:
            c = conn.cursor()
            query = "SELECT id FROM invoices WHERE invoicing_month = ?"
            params = [month]
            
            if private_only:
                query += " AND private_rechnung = 'invoice_needed'"
            
            c.execute(query, params)
            return [row[0] for row in c.fetchall()]

    @staticmethod
    def find_by_id(invoice_id: int):
        """Get complete invoice record by ID."""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,))
            row = c.fetchone()
            
            if not row:
                raise InvoiceNotFoundError(invoice_id)
            
            invoice_dict = dict(row)
            # Ensure invoice_id is always available as a key (not just 'id')
            if 'id' in invoice_dict and 'invoice_id' not in invoice_dict:
                invoice_dict['invoice_id'] = invoice_dict['id']
            
            return invoice_dict

    @staticmethod
    def find_all():
        """Get all invoices."""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM invoices")
            return [dict(row) for row in c.fetchall()]

    @staticmethod
    def update_invoice_number(invoice_id: int, invoice_number: int):
        """Atomically assign invoice number to an invoice."""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("UPDATE invoices SET invoice_number = ? WHERE id = ?", (invoice_number, invoice_id))
            conn.commit()
            logger.info(f"Assigned invoice number {invoice_number} to invoice {invoice_id}")

    @staticmethod
    def count_by_month(month: str):
        """Count invoices for a given month."""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM invoices WHERE invoicing_month = ?", (month,))
            return c.fetchone()[0]

    @staticmethod
    def find_by_patient_id(patient_id: int):
        """Get all invoices for a specific patient."""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM invoices WHERE patient_id = ? ORDER BY invoicing_month DESC", (patient_id,))
            return [dict(row) for row in c.fetchall()]

class PatientRepository:
    """Manages all patient-related database operations."""
    
    @staticmethod
    def find_by_id(patient_id: int):
        """Get patient by ID."""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM patients WHERE id = ?", (patient_id,))
            row = c.fetchone()
            
            if not row:
                raise PatientNotFoundError(f"Patient ID {patient_id}")
            
            return dict(row)

    @staticmethod
    def find_by_insurance_number(insurance_number: str):
        """Get patient by insurance/Krankenkasse number."""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM patients WHERE insurance_number = ?", (insurance_number,))
            row = c.fetchone()
            
            if not row:
                raise PatientNotFoundError(insurance_number)
            
            return dict(row)

    @staticmethod
    def find_all():
        """Get all patients."""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM patients ORDER BY name")
            return [dict(row) for row in c.fetchall()]

    @staticmethod
    def update_field(patient_id: int, field: str, value: str):
        """Update a single patient field."""
        allowed_fields = {"name", "birthdate", "care_level", "address", "debtor_number", "include_service_packet"}
        
        if field not in allowed_fields:
            raise ValueError(f"Cannot update field '{field}'")
        
        with get_db() as conn:
            c = conn.cursor()
            query = f"UPDATE patients SET {field} = ? WHERE id = ?"
            c.execute(query, (value, patient_id))
            conn.commit()
            logger.info(f"Updated patient {patient_id} field '{field}'")

    @staticmethod
    def count():
        """Total patient count."""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM patients")
            return c.fetchone()[0]

    @staticmethod
    def find_missing_fields(month: str):
        """Find patients with incomplete data in a specific invoicing month."""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT DISTINCT p.id, p.name, p.address, p.debtor_number
                FROM patients p
                JOIN invoices i ON p.id = i.patient_id
                WHERE i.invoicing_month = ?
                  AND (p.address IS NULL OR p.address = '' OR p.debtor_number IS NULL OR p.debtor_number = '')
            """, (month,))
            return [dict(row) for row in c.fetchall()]

class ServiceRepository:
    """Manages all service/Leistung-related database operations."""
    
    @staticmethod
    def find_by_code(code: str):
        """Get service by code."""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM services WHERE code = ?", (code,))
            row = c.fetchone()
            return dict(row) if row else None

    @staticmethod
    def find_by_invoice_id(invoice_id: int):
        """Get all services for an invoice."""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT quantity, code, description, unit_price, total_price 
                FROM services 
                WHERE invoice_id = ? 
                ORDER BY code
            """, (invoice_id,))
            
            return [
                {
                    "quantity": r[0],
                    "code": r[1],
                    "description": r[2],
                    "unit_price": r[3],
                    "total_price": r[4],
                }
                for r in c.fetchall()
            ]

    @staticmethod
    def count_by_invoice(invoice_id: int):
        """Count services for an invoice."""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM services WHERE invoice_id = ?", (invoice_id,))
            return c.fetchone()[0]

    @staticmethod
    def find_all_codes():
        """Get all unique service codes in the system."""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT DISTINCT code FROM services ORDER BY code")
            return [row[0] for row in c.fetchall()]

    @staticmethod
    def count_total():
        """Total service line items in system."""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM services")
            return c.fetchone()[0]


class CareRecordRepository:
    """Manages non-billable care records (SGBV, Verhinderungspflege)."""
    
    @staticmethod
    def insert(patient_id: int, record_type: str, pflegekonto: str, care_period_begin: str, 
               care_period_end: str, services: list, created_at: str, origin_chunk_id: str = None):
        """Insert a care record with associated services."""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO care_records 
                (patient_id, record_type, pflegekonto, care_period_begin, care_period_end, 
                 created_at, origin_chunk_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (patient_id, record_type, pflegekonto, care_period_begin, care_period_end,
                  created_at, origin_chunk_id))
            
            record_id = c.lastrowid
            
            # Insert associated services
            for service in services:
                c.execute("""
                    INSERT INTO care_services (care_record_id, quantity, code, description, unit_price, total_price)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (record_id, service.get("quantity"), service.get("code"), 
                      service.get("description"), service.get("unit_price"), service.get("total_price")))
            
            conn.commit()
            return record_id

    @staticmethod
    def find_by_id(record_id: int):
        """Get a care record with its services."""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT id, patient_id, record_type, pflegekonto, care_period_begin, 
                       care_period_end, created_at, origin_chunk_id
                FROM care_records 
                WHERE id = ?
            """, (record_id,))
            row = c.fetchone()
            if not row:
                return None
            
            record = dict(row)
            
            # Get associated services
            c.execute("""
                SELECT quantity, code, description, unit_price, total_price
                FROM care_services 
                WHERE care_record_id = ?
            """, (record_id,))
            record['services'] = [dict(s) for s in c.fetchall()]
            
            return record

    @staticmethod
    def find_by_patient(patient_id: int):
        """Get all care records for a patient with their services."""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT id, patient_id, record_type, pflegekonto, care_period_begin, 
                       care_period_end, created_at, origin_chunk_id
                FROM care_records 
                WHERE patient_id = ?
                ORDER BY care_period_begin DESC
            """, (patient_id,))
            
            records = []
            for row in c.fetchall():
                record = dict(row)
                # Get services for this record
                c.execute("""
                    SELECT quantity, code, description, unit_price, total_price
                    FROM care_services 
                    WHERE care_record_id = ?
                """, (record['id'],))
                record['services'] = [dict(s) for s in c.fetchall()]
                records.append(record)
            
            return records

    @staticmethod
    def find_by_type(record_type: str):
        """Get all records of a specific type (SGBV, Verhinderungspflege) with their services."""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT cr.id, cr.patient_id, p.name, cr.record_type, cr.pflegekonto, 
                       cr.care_period_begin, cr.care_period_end, cr.created_at
                FROM care_records cr
                JOIN patients p ON cr.patient_id = p.id
                WHERE cr.record_type = ?
                ORDER BY cr.care_period_begin DESC
            """, (record_type,))
            
            records = []
            for row in c.fetchall():
                record = dict(row)
                # Get services for this record
                c.execute("""
                    SELECT quantity, code, description, unit_price, total_price
                    FROM care_services 
                    WHERE care_record_id = ?
                """, (record['id'],))
                record['services'] = [dict(s) for s in c.fetchall()]
                records.append(record)
            
            return records

    @staticmethod
    def count_by_type(record_type: str):
        """Count records of a specific type."""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM care_records WHERE record_type = ?", (record_type,))
            return c.fetchone()[0]


class BillingSummaryRepository:
    """Repository for billing summary operations."""
    
    @staticmethod
    def insert(abrechnungsmonat: str, submitted_invoices_count: int, submitted_invoices_amount: float, created_at: str = None):
        """Insert or aggregate billing summary. Accumulates counts/amounts for the same month across multiple PDFs."""
        if created_at is None:
            created_at = datetime.now().isoformat()
        
        with get_db() as conn:
            c = conn.cursor()
            # Check if month already exists
            c.execute(
                "SELECT id, submitted_invoices_count, submitted_invoices_amount FROM billing_summary WHERE abrechnungsmonat = ?",
                (abrechnungsmonat,)
            )
            existing = c.fetchone()
            
            if existing:
                # Update: AGGREGATE (add to existing counts and amounts)
                existing_id, existing_count, existing_amount = existing
                new_count = existing_count + submitted_invoices_count
                new_amount = existing_amount + submitted_invoices_amount
                
                c.execute("""
                    UPDATE billing_summary
                    SET submitted_invoices_count = ?,
                        submitted_invoices_amount = ?
                    WHERE abrechnungsmonat = ?
                """, (new_count, new_amount, abrechnungsmonat))
                conn.commit()
                return existing_id
            else:
                # Insert: FIRST PDF for this month
                c.execute("""
                    INSERT INTO billing_summary (abrechnungsmonat, submitted_invoices_count, submitted_invoices_amount, created_at)
                    VALUES (?, ?, ?, ?)
                """, (abrechnungsmonat, submitted_invoices_count, submitted_invoices_amount, created_at))
                conn.commit()
                return c.lastrowid
    
    @staticmethod
    def find_by_month(abrechnungsmonat: str):
        """Find billing summary by month."""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM billing_summary WHERE abrechnungsmonat = ?", (abrechnungsmonat,))
            row = c.fetchone()
            if row:
                return dict(zip([desc[0] for desc in c.description], row))
            return None
    
    @staticmethod
    def get_all():
        """Retrieve all billing summaries."""
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM billing_summary ORDER BY abrechnungsmonat DESC")
            return [dict(zip([desc[0] for desc in c.description], row)) for row in c.fetchall()]
