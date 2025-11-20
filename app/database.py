import logging
from datetime import datetime
from app.db.config import enable_wal_mode
from app.db.connection import get_db, DB_PATH
from app.utils.parsing import GermanDecimalParser
# Repository imports for refactored queries
from app.db.repositories import InvoiceRepository, PatientRepository, ServiceRepository

logger = logging.getLogger(__name__)

def init_db():
    # Enable WAL mode for better concurrency on local installs
    enable_wal_mode(DB_PATH)
    
    with get_db() as conn:
        c = conn.cursor()
        
        # Enforce foreign key constraints
        c.execute("PRAGMA foreign_keys = ON")
        
        c.execute("""
        CREATE TABLE IF NOT EXISTS patients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            birthdate TEXT,
            insurance_number TEXT UNIQUE,
            care_level TEXT,
            address TEXT,
            debtor_number TEXT
        )
        """)

        # Ensure new column `include_service_packet` exists for per-patient service packet toggle
        # Use PRAGMA to check existing columns and ALTER TABLE to add the column if missing
        c.execute("PRAGMA table_info(patients)")
        cols = [r[1] for r in c.fetchall()]
        if "include_service_packet" not in cols:
            try:
                c.execute("ALTER TABLE patients ADD COLUMN include_service_packet INTEGER DEFAULT 0")
                logger.info("Added 'include_service_packet' column to patients table")
            except Exception as e:
                logger.warning(f"Could not add include_service_packet column: {e}")

        c.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER,
            care_range_begin TEXT,
            care_range_end TEXT,
            sum_covered TEXT,
            sum_total TEXT,
            amount_owed TEXT,
            created_at TEXT,
            care_account TEXT,
            invoicing_month TEXT,
            invoice_number INTEGER,
            origin_chunk_id TEXT,
            FOREIGN KEY(patient_id) REFERENCES patients(id)
        )
        """)
        c.execute("PRAGMA table_info(invoices)")
        invoice_cols = [r[1] for r in c.fetchall()]
        if "origin_chunk_id" not in invoice_cols:
            try:
                c.execute("ALTER TABLE invoices ADD COLUMN origin_chunk_id TEXT")
                logger.info("Added 'origin_chunk_id' column to invoices table")
            except Exception as e:
                logger.warning(f"Could not add origin_chunk_id column: {e}")

        c.execute("""
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER,
            quantity TEXT,
            code TEXT,
            description TEXT,
            unit_price TEXT,
            total_price TEXT,
            FOREIGN KEY(invoice_id) REFERENCES invoices(id)
        )
        """)
        
        # Create indexes for faster queries
        c.execute("CREATE INDEX IF NOT EXISTS idx_invoices_month ON invoices(invoicing_month)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_invoices_patient ON invoices(patient_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(private_rechnung)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_invoices_month_status ON invoices(invoicing_month, private_rechnung)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_services_invoice ON services(invoice_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_patients_insurance ON patients(insurance_number)")

        # Minimal chunks table for RAG metadata (embeddings stored externally in Chroma)
        c.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id TEXT PRIMARY KEY,
            patient_name TEXT,
            source_pdf TEXT,
            text_preview TEXT,
            created_at TEXT
        )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_chunks_patient ON chunks(patient_name)")

        conn.commit()

def insert_structured_data(data, origin_chunk_id: str | None = None):
    with get_db() as conn:
        c = conn.cursor()

        patient = data["patient"]
        invoice = data["invoice"]
        services = data["services"]

        if "care_account" not in invoice:
            invoice["care_account"] = patient.get("pflege_konto", "")

        invoice["summe_covered"] = f"{GermanDecimalParser.parse(invoice['summe_covered']):.2f}"
        invoice["summe_total"] = f"{GermanDecimalParser.parse(invoice['summe_total']):.2f}"
        invoice["amount_owed"] = f"{GermanDecimalParser.parse(invoice['summe_total']) - GermanDecimalParser.parse(invoice['summe_covered']):.2f}"

        try:
            total_val = float(invoice["summe_total"])
            covered_val = float(invoice["summe_covered"])
        except ValueError as e:
            logger.error(f"Invalid invoice totals for patient {patient.get('name', '[unknown]')}: sum_covered={invoice.get('summe_covered')}, sum_total={invoice.get('summe_total')} — Error: {e}")
            return

        amount_owed = total_val - covered_val
        invoice["amount_owed"] = f"{amount_owed:,.2f}".replace('.', ',').replace(',', '.', 1)

        for s in services:
            s["quantity"] = s["quantity"].replace(',', '.')
            s["unit_price"] = GermanDecimalParser.parse(s["unit_price"])
            s["total_price"] = GermanDecimalParser.parse(s["total_price"])

        c.execute("""
            INSERT OR IGNORE INTO patients (name, birthdate, insurance_number, care_level, include_service_packet)
            VALUES (?, ?, ?, ?, ?)
        """, (
            patient["name"],
            patient["birthdate"],
            patient["insurance_number"],
            patient["care_level"],
            int(bool(patient.get("include_service_packet", 0)))
        ))

        c.execute("SELECT id FROM patients WHERE insurance_number = ?", (patient["insurance_number"],))
        row = c.fetchone()
        if not row:
            logger.error(f"Patient insertion failed for {patient.get('name', '[unknown]')} (insurance_number={patient.get('insurance_number')}): record not found after INSERT OR IGNORE. Check for constraint violations.")
            return
        patient_id = row[0]

        # If the structured input supplied an explicit include_service_packet flag, ensure DB reflects it
        if "include_service_packet" in patient:
            try:
                c.execute("UPDATE patients SET include_service_packet = ? WHERE id = ?", (
                    int(bool(patient.get("include_service_packet"))),
                    patient_id
                ))
            except Exception:
                logger.debug("Could not update include_service_packet for patient id %s", patient_id)

        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        c.execute("""
            INSERT INTO invoices (
                patient_id, care_range_begin, care_range_end,
                sum_covered, sum_total, amount_owed, created_at,
                care_account, invoicing_month, origin_chunk_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            patient_id,
            invoice["pflegezeitraum_beginn"],
            invoice["pflegezeitraum_ende"],
            invoice["summe_covered"],
            invoice["summe_total"],
            invoice["amount_owed"],
            created_at,
            invoice["care_account"],
            invoice.get("abrechnungsmonat"),
            origin_chunk_id
        ))

        invoice_id = c.lastrowid

        for s in services:
            c.execute("""
                INSERT INTO services (invoice_id, quantity, code, description, unit_price, total_price)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                invoice_id,
                s["quantity"],
                s["code"],
                s["description"],
                s["unit_price"],
                s["total_price"]
            ))

        conn.commit()


def insert_chunk(chunk_id: str, patient_name: str | None, source_pdf: str | None, text_preview: str | None, created_at: str):
    """
    Insert a minimal chunk metadata row into the chunks table.
    Embeddings are stored externally (Chroma) and referenced by chunk_id.
    """
    with get_db() as conn:
        c = conn.cursor()
        try:
            c.execute(
                "INSERT OR IGNORE INTO chunks (id, patient_name, source_pdf, text_preview, created_at) VALUES (?, ?, ?, ?, ?)",
                (chunk_id, patient_name or "", source_pdf or "", text_preview or "", created_at),
            )
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to insert chunk {chunk_id}: {e}")


def update_chunk_patient(chunk_id: str | None, patient_name: str | None):
    """
    Keep chunk metadata in sync once we know the patient name.
    """
    if not chunk_id:
        return

    with get_db() as conn:
        c = conn.cursor()
        try:
            c.execute(
                "UPDATE chunks SET patient_name = ? WHERE id = ?",
                (patient_name or "", chunk_id),
            )
            if c.rowcount == 0:
                logger.debug(f"No chunk row updated for {chunk_id} (maybe it does not exist yet)")
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to update chunk {chunk_id} patient_name: {e}")

def get_private_invoice_cases(invoicing_month=None, invoice_id=None):
    cases = []
    if invoice_id:
        invoice = InvoiceRepository.find_by_id(invoice_id)
        patient = PatientRepository.find_by_insurance_number(invoice["patient_id"])
        services = ServiceRepository.find_by_invoice_id(invoice_id)
        case = {
            "invoice": invoice,
            "patient": patient,
            "services": services
        }
        cases.append(case)
    else:
        invoice_ids = InvoiceRepository.find_by_month(invoicing_month, private_only=True)
        for inv_id in invoice_ids:
            invoice = InvoiceRepository.find_by_id(inv_id)
            patient = PatientRepository.find_by_insurance_number(invoice["patient_id"])
            services = ServiceRepository.find_by_invoice_id(inv_id)
            case = {
                "invoice": invoice,
                "patient": patient,
                "services": services
            }
            cases.append(case)
    return cases

def check_missing_patient_fields(invoicing_month, auto_fix=False):
    """
    Check for patients missing required fields (address, debtor_number).
    
    Args:
        invoicing_month: Month to check (MMYYYY format)
        auto_fix: If True, automatically use placeholder values. If False, just log warnings.
    """
    with get_db() as conn:
        c = conn.cursor()

        c.execute("""
            SELECT DISTINCT p.id, p.name, p.insurance_number, p.address, p.debtor_number
            FROM patients p
            JOIN invoices i ON p.id = i.patient_id
            WHERE i.invoicing_month = ?
        """, (invoicing_month,))

        patients = c.fetchall()

        for patient_id, name, insurance_number, address, debtor in patients:
            if address and debtor:
                continue

            logger.warning(f"Patient '{name}' ({insurance_number}) is missing address or debtor number.")

            if auto_fix:
                # Use placeholders instead of prompting
                address = address or "[ADRESSE ERFORDERLICH]"
                debtor = debtor or "[SCHULDNUMMER ERFORDERLICH]"
                
                c.execute("""
                    UPDATE patients SET address = ?, debtor_number = ? WHERE id = ?
                """, (address, debtor, patient_id))
                logger.info(f"Patient {name}: auto-fixed with placeholder values.")
            else:
                logger.warning(f"  - Address: {'MISSING' if not address else 'OK'}")
                logger.warning(f"  - Debtor Number: {'MISSING' if not debtor else 'OK'}")

        conn.commit()

def check_service_fields(auto_fix=False):
    """
    Check for services with quantity mismatches.
    
    Args:
        auto_fix: If True, automatically fix mismatches. If False, just log warnings.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
        SELECT id, invoice_id, quantity, code, description, unit_price, total_price
        FROM services
        """)

        rows = cursor.fetchall()
        
        for row in rows:
            service_id, invoice_id, quantity, service_code, description, unit_price, total_price = row
            unit_price_float = float(unit_price.replace(",", "."))
            total_price_float = float(total_price.replace(",", "."))
            quantity_float = float(quantity.replace(",", "."))

            calculated_quantity = total_price_float / unit_price_float

            percentage_diff = abs(calculated_quantity - quantity_float) / calculated_quantity * 100

            if percentage_diff < 0.5:
                continue  # close enough

            logger.warning(f"Service ID {service_id}: quantity mismatch (expected {calculated_quantity:.2f}, got {quantity_float}).")
            
            if auto_fix:
                correct_quantity_str = f"{calculated_quantity:.2f}".replace('.', ',')
                cursor.execute("""
                    UPDATE services
                    SET quantity = ?
                    WHERE id = ?
                """, (correct_quantity_str, service_id))
                logger.info(f"Service ID {service_id}: auto-fixed quantity to {correct_quantity_str}.")
            else:
                logger.warning(f"  - Invoice ID: {invoice_id}")
                logger.warning(f"  - Service Code: {service_code}")
                logger.warning(f"  - Expected Quantity: {calculated_quantity:.2f}")
                logger.warning(f"  - Current Quantity: {quantity_float}")

        conn.commit()

def _ensure_private_rechnung_column():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("PRAGMA table_info(invoices)")
        cols = [row[1] for row in c.fetchall()]
        if "private_rechnung" not in cols:
            # Add column as TEXT with default NULL (SQLite: ADD COLUMN always appends as last column)
            c.execute("ALTER TABLE invoices ADD COLUMN private_rechnung TEXT DEFAULT NULL")
            conn.commit()

def mark_month_ready_for_generation(invoicing_month: str, only_positive: bool = False) -> int:
    """
    Set private_rechnung based on amount_owed for all invoices of the given invoicing_month.
    - If amount_owed > 0: private_rechnung = 'invoice_needed'
    - If amount_owed = 0: private_rechnung = 'covered_insurance'
    If only_positive=True, only mark invoices with amount_owed > 0.
    Returns number of rows updated.
    """
    _ensure_private_rechnung_column()
    with get_db() as conn:
        c = conn.cursor()
        before = conn.total_changes

        # Update all invoices with the appropriate status based on amount_owed
        c.execute(
            """
            UPDATE invoices
               SET private_rechnung = CASE 
                   WHEN CAST(REPLACE(REPLACE(amount_owed, '.', ''), ',', '.') AS REAL) > 0.0 
                   THEN 'invoice_needed'
                   WHEN CAST(REPLACE(REPLACE(amount_owed, '.', ''), ',', '.') AS REAL) = 0.0 
                   THEN 'covered_insurance'
                   ELSE NULL
               END
             WHERE invoicing_month = ?
            """,
            (invoicing_month,),
        )

        conn.commit()
        changes = conn.total_changes - before
        logger.info(f"Marked {changes} invoices in {invoicing_month} with appropriate status (invoice_needed or covered_insurance).")
        return changes
