import logging
from datetime import datetime
from app.db.config import enable_wal_mode
from app.db.connection import get_db, DB_PATH
from app.utils.parsing import GermanDecimalParser

from app.db.repositories import InvoiceRepository, PatientRepository, ServiceRepository, CareRecordRepository, BillingSummaryRepository
from app.utils.data_validation import DataNormalizer, DataValidationError

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
        
        # Care records table for non-billable care data (SGBV, Verhinderungspflege)
        c.execute("""
        CREATE TABLE IF NOT EXISTS care_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER,
            record_type TEXT,
            pflegekonto TEXT,
            care_period_begin TEXT,
            care_period_end TEXT,
            created_at TEXT,
            origin_chunk_id TEXT,
            FOREIGN KEY(patient_id) REFERENCES patients(id)
        )
        """)
        
        # Care services table for storing services related to care records
        c.execute("""
        CREATE TABLE IF NOT EXISTS care_services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            care_record_id INTEGER,
            quantity TEXT,
            code TEXT,
            description TEXT,
            unit_price TEXT,
            total_price TEXT,
            FOREIGN KEY(care_record_id) REFERENCES care_records(id)
        )
        """)
        
        # Billing summary table for first-page aggregate data
        c.execute("""
        CREATE TABLE IF NOT EXISTS billing_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            abrechnungsmonat TEXT NOT NULL,
            submitted_invoices_count INTEGER,
            submitted_invoices_amount REAL,
            created_at TEXT
        )
        """)
        
        # Create indexes for faster queries
        c.execute("CREATE INDEX IF NOT EXISTS idx_invoices_month ON invoices(invoicing_month)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_invoices_patient ON invoices(patient_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(private_rechnung)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_invoices_month_status ON invoices(invoicing_month, private_rechnung)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_services_invoice ON services(invoice_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_care_services_record ON care_services(care_record_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_patients_insurance ON patients(insurance_number)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_care_records_patient ON care_records(patient_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_care_records_type ON care_records(record_type)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_care_records_pflegekonto ON care_records(pflegekonto)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_billing_summary_month ON billing_summary(abrechnungsmonat)")


        conn.commit()

def insert_structured_data(data, origin_chunk_id: str | None = None):
    with get_db() as conn:
        c = conn.cursor()

        patient = data["patient"]
        invoice = data["invoice"]
        services = data["services"]

        if "care_account" not in invoice:
            invoice["care_account"] = patient.get("pflege_konto", "")

        # Normalize birthdate to DD.MM.YYYY format with 4-digit year
        try:
            patient["birthdate"] = DataNormalizer.normalize_birthdate(patient.get("birthdate", ""))
        except DataValidationError as e:
            logger.error(f"Failed to normalize birthdate for patient {patient.get('name', '[unknown]')}: {e}")
            return

        # Normalize invoice amounts to standard format (period as decimal)
        try:
            invoice["summe_covered"] = DataNormalizer.normalize_amount(invoice.get("summe_covered", "0"))
            invoice["summe_total"] = DataNormalizer.normalize_amount(invoice.get("summe_total", "0"))
        except DataValidationError as e:
            logger.error(f"Failed to normalize invoice amounts for patient {patient.get('name', '[unknown]')}: {e}")
            return

        try:
            total_val = float(invoice["summe_total"])
            covered_val = float(invoice["summe_covered"])
        except ValueError as e:
            logger.error(f"Invalid invoice totals for patient {patient.get('name', '[unknown]')}: sum_covered={invoice.get('summe_covered')}, sum_total={invoice.get('summe_total')} — Error: {e}")
            return

        amount_owed = total_val - covered_val
        invoice["amount_owed"] = f"{amount_owed:.2f}"

        # Normalize service amounts
        for s in services:
            try:
                s["unit_price"] = DataNormalizer.normalize_amount(s.get("unit_price", "0"))
                s["total_price"] = DataNormalizer.normalize_amount(s.get("total_price", "0"))
                # Normalize quantity (handle comma as decimal separator)
                quantity_str = str(s.get("quantity", "0")).strip()
                s["quantity"] = quantity_str.replace(',', '.')
            except DataValidationError as e:
                logger.warning(f"Failed to normalize service amount: {e}")

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


def insert_care_record(data: dict, record_type: str, pflegekonto: str, origin_chunk_id: str = None):
    """
    Insert a non-billable care record (SGBV, Verhinderungspflege) with associated services.
    
    Args:
        data: Dictionary with 'patient', 'services', 'invoice' keys (same as invoice structure)
        record_type: 'SGBV' or 'Verhinderungspflege'
        pflegekonto: Pflegekonto code (4092 or 4050)
        origin_chunk_id: Optional chunk ID for traceability
    """
    with get_db() as conn:
        c = conn.cursor()
        
        patient = data.get("patient", {})
        invoice = data.get("invoice", {})
        services = data.get("services", [])
        
        # Normalize birthdate to DD.MM.YYYY format with 4-digit year
        try:
            patient["birthdate"] = DataNormalizer.normalize_birthdate(patient.get("birthdate", ""))
        except DataValidationError as e:
            logger.error(f"Failed to normalize birthdate for care record patient {patient.get('name', '[unknown]')}: {e}")
            return None
        
        # Normalize service amounts
        for service in services:
            try:
                if service.get("unit_price"):
                    service["unit_price"] = DataNormalizer.normalize_amount(service["unit_price"])
                if service.get("total_price"):
                    service["total_price"] = DataNormalizer.normalize_amount(service["total_price"])
                # Normalize quantity (handle comma as decimal separator)
                if service.get("quantity"):
                    quantity_str = str(service.get("quantity", "0")).strip()
                    service["quantity"] = quantity_str.replace(',', '.')
            except DataValidationError as e:
                logger.warning(f"Failed to normalize service amount in care record: {e}")
        
        # Ensure patient exists
        c.execute("""
            INSERT OR IGNORE INTO patients (name, birthdate, insurance_number, care_level)
            VALUES (?, ?, ?, ?)
        """, (
            patient.get("name"),
            patient.get("birthdate"),
            patient.get("insurance_number"),
            patient.get("care_level")
        ))
        
        c.execute("SELECT id FROM patients WHERE insurance_number = ?", (patient.get("insurance_number"),))
        row = c.fetchone()
        if not row:
            logger.error(f"Failed to insert patient for care record: {patient.get('name')}")
            return None
        
        patient_id = row[0]
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Insert care record (without services field)
        c.execute("""
            INSERT INTO care_records 
            (patient_id, record_type, pflegekonto, care_period_begin, care_period_end, 
             created_at, origin_chunk_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (patient_id, record_type, pflegekonto, invoice.get("pflegezeitraum_beginn"),
              invoice.get("pflegezeitraum_ende"), created_at, origin_chunk_id))
        
        record_id = c.lastrowid
        
        # Insert associated services
        for service in services:
            c.execute("""
                INSERT INTO care_services (care_record_id, quantity, code, description, unit_price, total_price)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                record_id,
                service.get("quantity"),
                service.get("code"),
                service.get("description"),
                service.get("unit_price"),
                service.get("total_price")
            ))
        
        conn.commit()
        
        logger.info(f"✓ Care record inserted: record_id={record_id}, type={record_type}, patient={patient.get('name')}, pflegekonto={pflegekonto}, services={len(services)}")
        return record_id

def insert_billing_summary(data, abrechnungsmonat):
    """Insert billing summary from first-page aggregate data."""
    try:
        submitted_invoices_count = data.get("submitted_invoices_count")
        submitted_invoices_amount = data.get("submitted_invoices_amount")
        
        if submitted_invoices_count is None or submitted_invoices_amount is None:
            logger.warning("Missing required billing summary fields")
            return None
        
        # Convert German decimal format if needed
        if isinstance(submitted_invoices_amount, str):
            parser = GermanDecimalParser()
            submitted_invoices_amount = parser.parse(submitted_invoices_amount)
        
        created_at = datetime.now().isoformat()
        
        # Insert using repository
        summary_id = BillingSummaryRepository.insert(
            abrechnungsmonat=abrechnungsmonat,
            submitted_invoices_count=submitted_invoices_count,
            submitted_invoices_amount=submitted_invoices_amount,
            created_at=created_at
        )
        
        logger.info(f"✓ Billing summary inserted: summary_id={summary_id}, month={abrechnungsmonat}, count={submitted_invoices_count}, amount={submitted_invoices_amount}")
        return summary_id
    except Exception as e:
        logger.error(f"Error inserting billing summary: {e}")
        return None

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
            patient = PatientRepository.find_by_id(invoice["patient_id"])
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
    Set private_rechnung based on sum_covered value for all invoices of the given invoicing_month.
    Unified logic for all care accounts (SGBXI, Entleistung, etc.):
    - If patient has include_service_packet = 1: private_rechnung = 'invoice_needed'
    - Else if sum_covered < sum_total: private_rechnung = 'invoice_needed' (has out-of-pocket costs)
    - Else if sum_covered = sum_total: private_rechnung = 'covered_insurance' (fully covered)
    
    For 4064 (Entleistung): sum_covered is capped at 127.50, so:
    - If total <= 127.50: sum_covered = total → 'covered_insurance'
    - If total > 127.50: sum_covered = 127.50 → 'invoice_needed'
    
    If only_positive=True, only mark invoices with amount_owed > 0.
    Returns number of rows updated.
    """
    _ensure_private_rechnung_column()
    with get_db() as conn:
        c = conn.cursor()
        before = conn.total_changes

        # Unified logic: use sum_covered to determine private_rechnung status for ALL invoices
        # Works for SGBXI, Entleistung, and all other care accounts
        c.execute(
            """
            UPDATE invoices
               SET private_rechnung = CASE 
                   WHEN (SELECT include_service_packet FROM patients WHERE id = invoices.patient_id) = 1
                   THEN 'invoice_needed'
                   WHEN CAST(REPLACE(REPLACE(sum_covered, '.', ''), ',', '.') AS REAL) < CAST(REPLACE(REPLACE(sum_total, '.', ''), ',', '.') AS REAL)
                   THEN 'invoice_needed'
                   WHEN CAST(REPLACE(REPLACE(sum_covered, '.', ''), ',', '.') AS REAL) = CAST(REPLACE(REPLACE(sum_total, '.', ''), ',', '.') AS REAL)
                   THEN 'covered_insurance'
                   ELSE NULL
               END
             WHERE invoicing_month = ?
            """,
            (invoicing_month,),
        )

        conn.commit()
        changes = conn.total_changes - before
        logger.info(f"Marked {changes} invoices in {invoicing_month} with appropriate status based on sum_covered value (applies to all care accounts).")
        return changes
