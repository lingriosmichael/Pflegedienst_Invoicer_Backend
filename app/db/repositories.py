"""
Repository classes for database access.
Implements atomic invoice number generation and centralizes queries.
"""

from app.db.connection import get_db
from app.exceptions import InvoiceNotFoundError

class InvoiceRepository:
    @staticmethod
    def get_next_invoice_number():
        """
        Atomically get the next invoice number.
        Creates invoice_sequences table if not exists.
        """
        with get_db() as conn:
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS invoice_sequences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    last_number INTEGER NOT NULL
                )
            """)
            c.execute("SELECT last_number FROM invoice_sequences WHERE id=1")
            row = c.fetchone()
            if row is None:
                c.execute("INSERT INTO invoice_sequences (id, last_number) VALUES (1, 1)")
                conn.commit()
                return 1
            next_number = row[0] + 1
            c.execute("UPDATE invoice_sequences SET last_number=? WHERE id=1", (next_number,))
            conn.commit()
            return next_number

    @staticmethod
    def find_by_month(month: str, private_only: bool = False):
        with get_db() as conn:
            c = conn.cursor()
            query = "SELECT id FROM invoices WHERE abrechnungsmonat=?"
            params = [month]
            if private_only:
                query += " AND is_private=1"
            c.execute(query, params)
            return [row[0] for row in c.fetchall()]

    @staticmethod
    def find_by_id(invoice_id: int):
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,))
            row = c.fetchone()
            if not row:
                raise InvoiceNotFoundError(invoice_id)
            return dict(row)

    @staticmethod
    def mark_ready(month: str, only_positive: bool = False):
        with get_db() as conn:
            c = conn.cursor()
            query = "UPDATE invoices SET ready=1 WHERE abrechnungsmonat=?"
            params = [month]
            if only_positive:
                query += " AND summe_total > 0"
            c.execute(query, params)
            conn.commit()
            return c.rowcount

class PatientRepository:
    @staticmethod
    def find_by_insurance_number(insurance_number: str):
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM patients WHERE insurance_number=?", (insurance_number,))
            row = c.fetchone()
            if not row:
                from app.exceptions import PatientNotFoundError
                raise PatientNotFoundError(insurance_number)
            return dict(row)

class ServiceRepository:
    @staticmethod
    def find_by_code(code: str):
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM services WHERE code=?", (code,))
            row = c.fetchone()
            if not row:
                return None
            return dict(row)

    @staticmethod
    def find_by_invoice_id(invoice_id: int):
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT quantity, code, description, unit_price, total_price FROM services WHERE invoice_id=?", (invoice_id,))
            rows = c.fetchall()
            return [
                {
                    "quantity": r[0],
                    "code": r[1],
                    "description": r[2],
                    "unit_price": r[3],
                    "total_price": r[4],
                }
                for r in rows
            ]
