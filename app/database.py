import logging
from datetime import datetime
from app.db.config import enable_wal_mode
from app.db.connection import get_db, DB_PATH
from app.utils.parsing import GermanDecimalParser, generate_id

from app.db.repositories import InvoiceRepository, PatientRepository, ServiceRepository, CareRecordRepository, BillingSummaryRepository
from app.utils.data_validation import DataNormalizer, DataValidationError

logger = logging.getLogger(__name__)

def init_db():
    """
    Initialize database - creates unified schema if needed.
    Note: The unified schema is defined in schema_clean.sql and deployed there.
    This function just ensures WAL mode is enabled; table creation should use schema_clean.sql
    """
    # Enable WAL mode for better concurrency on local installs
    enable_wal_mode(DB_PATH)
    
    with get_db() as conn:
        c = conn.cursor()
        
        # Enforce foreign key constraints
        c.execute("PRAGMA foreign_keys = ON")
        
        # Tables should be created via schema_clean.sql
        # This is just a safety check that the unified schema exists
        logger.info("✓ Database initialized with WAL mode enabled. Unified schema should be deployed via schema_clean.sql")
        
        conn.commit()

def insert_structured_data(data, origin_chunk_id: str | None = None, invoicing_month: str | None = None):
    """
    Insert invoice data into new unified schema.
    Maps to: patient_profiles, care_events (SGBXI/Entleistung), billing_details, care_services_new
    
    Args:
        data: Structured invoice data
        origin_chunk_id: Optional chunk ID for tracking
        invoicing_month: Billing month for this data (MMYYYY format, e.g., "012025")
    """
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
            logger.error(f"❌ Failed to normalize birthdate for patient {patient.get('name', '[unknown]')}: {e}")
            return

        # Normalize invoice amounts to numeric (REAL) values
        try:
            covered_float = float(DataNormalizer.normalize_amount(invoice.get("summe_covered", "0")))
            total_float = float(DataNormalizer.normalize_amount(invoice.get("summe_total", "0")))
            invoice["summe_covered"] = covered_float
            invoice["summe_total"] = total_float
        except DataValidationError as e:
            logger.error(f"❌ Failed to normalize invoice amounts for patient {patient.get('name', '[unknown]')}: {e}")
            return

        try:
            total_val = float(invoice["summe_total"])
            covered_val = float(invoice["summe_covered"])
        except ValueError as e:
            logger.error(f"❌ Invalid invoice totals for patient {patient.get('name', '[unknown]')}: sum_covered={invoice.get('summe_covered')}, sum_total={invoice.get('summe_total')} — Error: {e}")
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

        # Generate IDs for new unified schema
        from app.utils.parsing import generate_id
        care_event_id = generate_id("evt")
        
        # Check if patient already exists by insurance_number
        c.execute("SELECT patient_id FROM patient_profiles WHERE org_id = ? AND insurance_number = ?", 
                  ('org_default', patient.get("insurance_number")))
        existing = c.fetchone()
        
        if existing:
            # Patient exists - reuse their ID and update with new data (care_level, address)
            patient_id = existing[0]
            c.execute("""
                UPDATE patient_profiles 
                SET patient_name = ?, date_of_birth = ?, care_level = ?, 
                    include_service_packet = ?, updated_at = ?
                WHERE patient_id = ?
            """, (
                patient.get("name"),
                patient.get("birthdate"),
                patient.get("care_level"),
                int(bool(patient.get("include_service_packet", 0))),
                datetime.now().isoformat(),
                patient_id
            ))
        else:
            # New patient - generate new ID and insert
            patient_id = generate_id("pat")
            c.execute("""
                INSERT INTO patient_profiles 
                (patient_id, org_id, patient_name, date_of_birth, insurance_number, care_level, include_service_packet, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                patient_id,
                'org_default',
                patient.get("name"),
                patient.get("birthdate"),
                patient.get("insurance_number"),
                patient.get("care_level"),
                int(bool(patient.get("include_service_packet", 0))),
                datetime.now().isoformat()
            ))

        # Determine event_type based on care_account (Pflegekonto)
        care_account = invoice.get("care_account", "")
        if care_account == "4062":
            event_type = "Consultation"
        elif care_account == "4064":
            event_type = "Entleistung"
        elif care_account == "4050":
            event_type = "Verhinderungspflege"
        elif care_account in ["4010", "4020", "4030", "4040"]:
            event_type = "SGBXI"
        else:
            event_type = "SGBV"  # Default

        logger.info(f"→ Inserting care_event for {patient.get('name')} (insurance: {patient.get('insurance_number')}, care_account: {care_account}, event_type: {event_type})")

        # Insert into care_events
        c.execute("""
            INSERT INTO care_events 
            (care_event_id, org_id, patient_id, event_type, period_start_date, period_end_date, 
             care_account, sum_covered, sum_total, invoicing_month, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            care_event_id,
            'org_default',
            patient_id,
            event_type,
            invoice.get("pflegezeitraum_beginn"),
            invoice.get("pflegezeitraum_ende"),
            care_account,
            invoice.get("summe_covered"),
            invoice.get("summe_total"),
            invoicing_month,
            datetime.now().isoformat()
        ))

        # Insert services
        for s in services:
            service_id = generate_id("svc")
            c.execute("""
                INSERT INTO care_services_new 
                (service_id, org_id, care_event_id, service_code, service_description, 
                 quantity_value, unit_price, line_total, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                service_id,
                'org_default',
                care_event_id,
                s.get("code"),
                s.get("description"),
                s.get("quantity"),
                s.get("unit_price"),
                s.get("total_price"),
                datetime.now().isoformat()
            ))

        # Create history record
        history_id = generate_id("hist")
        c.execute("""
            INSERT INTO care_event_history 
            (history_id, org_id, care_event_id, action, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            history_id,
            'org_default',
            care_event_id,
            'created',
            datetime.now().isoformat()
        ))

        conn.commit()
        logger.info(f"✓ Care event inserted: {care_event_id}, type={event_type}, patient={patient.get('name')}")


def insert_care_record(data: dict, record_type: str, pflegekonto: str, origin_chunk_id: str = None):
    """
    Insert a non-billable care record (SGBV, Verhinderungspflege) with associated services.
    Maps to: patient_profiles, care_events (event_type='SGBV'), care_services_new
    
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
        
        # Generate IDs for new unified schema
        from app.utils.parsing import generate_id
        patient_id = generate_id("pat")
        care_event_id = generate_id("evt")
        
        # Check if patient already exists by insurance_number
        c.execute("SELECT patient_id FROM patient_profiles WHERE org_id = ? AND insurance_number = ?", 
                  ('org_default', patient.get("insurance_number")))
        existing = c.fetchone()
        
        if existing:
            # Patient exists - reuse their ID and update with new data
            patient_id = existing[0]
            c.execute("""
                UPDATE patient_profiles 
                SET patient_name = ?, date_of_birth = ?, care_level = ?, updated_at = ?
                WHERE patient_id = ?
            """, (
                patient.get("name"),
                patient.get("birthdate"),
                patient.get("care_level"),
                datetime.now().isoformat(),
                patient_id
            ))
        else:
            # New patient - generate new ID and insert
            from app.utils.parsing import generate_id
            patient_id = generate_id("pat")
            c.execute("""
                INSERT INTO patient_profiles 
                (patient_id, org_id, patient_name, date_of_birth, insurance_number, care_level, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                patient_id,
                'org_default',
                patient.get("name"),
                patient.get("birthdate"),
                patient.get("insurance_number"),
                patient.get("care_level"),
                datetime.now().isoformat()
            ))
        
        # Normalize invoice amounts to numeric (REAL) values
        try:
            covered_float = float(DataNormalizer.normalize_amount(invoice.get("summe_covered", "0")))
            total_float = float(DataNormalizer.normalize_amount(invoice.get("summe_total", "0")))
        except DataValidationError as e:
            logger.warning(f"Failed to normalize invoice amounts for care record patient {patient.get('name', '[unknown]')}: {e}")
            covered_float = 0.0
            total_float = 0.0
        
        # Insert care event with the correct event_type based on record_type
        c.execute("""
            INSERT INTO care_events 
            (care_event_id, org_id, patient_id, event_type, period_start_date, period_end_date, 
             care_account, sum_covered, sum_total, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            care_event_id,
            'org_default',
            patient_id,
            record_type,  # Use the record_type (SGBV or Verhinderungspflege)
            invoice.get("pflegezeitraum_beginn"),
            invoice.get("pflegezeitraum_ende"),
            pflegekonto,  # Use the pflegekonto as care_account
            covered_float,
            total_float,
            datetime.now().isoformat()
        ))
        
        # Insert associated services
        for service in services:
            service_id = generate_id("svc")
            c.execute("""
                INSERT INTO care_services_new 
                (service_id, org_id, care_event_id, service_code, service_description,
                 quantity_value, unit_price, line_total, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                service_id,
                'org_default',
                care_event_id,
                service.get("code"),
                service.get("description"),
                service.get("quantity"),
                service.get("unit_price"),
                service.get("total_price"),
                datetime.now().isoformat()
            ))
        
        # Create history record
        history_id = generate_id("hist")
        c.execute("""
            INSERT INTO care_event_history 
            (history_id, org_id, care_event_id, action, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            history_id,
            'org_default',
            care_event_id,
            'created',
            datetime.now().isoformat()
        ))
        
        conn.commit()
        
        logger.info(f"✓ Care event inserted: {care_event_id}, type=SGBV, patient={patient.get('name')}, services={len(services)}")
        return care_event_id

def insert_billing_summary(data, abrechnungsmonat):
    """Insert or aggregate billing summary from first-page data. Accumulates counts/amounts for same month."""
    try:
        from app.utils.parsing import generate_id
        
        submitted_invoices_count = data.get("submitted_invoices_count")
        submitted_invoices_amount = data.get("submitted_invoices_amount")
        
        if submitted_invoices_count is None or submitted_invoices_amount is None:
            logger.warning("Missing required billing summary fields")
            return None
        
        # Convert German decimal format if needed
        if isinstance(submitted_invoices_amount, str):
            parser = GermanDecimalParser()
            submitted_invoices_amount = parser.parse(submitted_invoices_amount)
        
        with get_db() as conn:
            c = conn.cursor()
            # Check if summary already exists for this month
            c.execute(
                "SELECT summary_id, submitted_invoice_count, submitted_invoice_amount FROM billing_summary WHERE org_id = ? AND billing_month = ?",
                ('org_default', abrechnungsmonat)
            )
            existing = c.fetchone()
            
            if existing:
                # Update: AGGREGATE (add to existing counts and amounts)
                summary_id, existing_count, existing_amount = existing
                new_count = existing_count + submitted_invoices_count
                new_amount = existing_amount + submitted_invoices_amount
                
                c.execute("""
                    UPDATE billing_summary
                    SET submitted_invoice_count = ?,
                        submitted_invoice_amount = ?,
                        updated_at = ?
                    WHERE summary_id = ?
                """, (new_count, new_amount, datetime.now().isoformat(), summary_id))
                
                logger.info(f"✓ Billing summary updated (aggregated): month={abrechnungsmonat}, "
                           f"old=(count={existing_count}, amount={existing_amount}), "
                           f"new=(count={new_count}, amount={new_amount})")
            else:
                # Insert: FIRST PDF for this month
                summary_id = generate_id("sum")
                c.execute("""
                    INSERT INTO billing_summary
                    (summary_id, org_id, billing_month, submitted_invoice_count, 
                     submitted_invoice_amount, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    summary_id,
                    'org_default',
                    abrechnungsmonat,
                    submitted_invoices_count,
                    submitted_invoices_amount,
                    datetime.now().isoformat(),
                    datetime.now().isoformat()
                ))
                
                logger.info(f"✓ Billing summary created: {summary_id}, month={abrechnungsmonat}, count={submitted_invoices_count}, amount={submitted_invoices_amount}")
            
            conn.commit()
            return summary_id
    except Exception as e:
        logger.error(f"Error inserting/updating billing summary: {e}")
        return None

def get_private_invoice_cases(invoicing_month=None, invoice_id=None):
    """
    Get invoices needing private invoices from unified schema.
    Filters for event_type='SGBXI' (billable invoices only, excludes Consultation).
    """
    cases = []
    with get_db() as conn:
        c = conn.cursor()
        
        if invoice_id:
            # Get specific care event (must be billable type)
            c.execute("""
                SELECT ce.care_event_id, ce.patient_id, ce.invoicing_month, ce.invoice_number,
                       ce.amount_owed, ce.sum_covered, ce.sum_total, ce.care_range_begin, ce.care_range_end
                FROM care_events ce
                JOIN billing_details bd ON ce.care_event_id = bd.care_event_id
                WHERE ce.care_event_id = ? AND ce.event_type IN ('SGBXI', 'Entleistung')
            """, (invoice_id,))
            row = c.fetchone()
            if row:
                care_event_id, patient_id, month, inv_num, amt_owed, sum_cov, sum_tot, range_begin, range_end = row
                
                # Get patient
                c.execute("SELECT patient_id, patient_name, insurance_number, birthdate, care_level FROM patient_profiles WHERE patient_id = ?", (patient_id,))
                patient_row = c.fetchone()
                
                # Get services
                c.execute("SELECT service_id, code, description, quantity, unit_price, total_price FROM care_services_new WHERE care_event_id = ?", (care_event_id,))
                services_rows = c.fetchall()
                
                if patient_row:
                    case = {
                        "invoice": {
                            "id": care_event_id,
                            "invoicing_month": month,
                            "invoice_number": inv_num,
                            "amount_owed": amt_owed,
                            "sum_covered": sum_cov,
                            "sum_total": sum_tot,
                            "care_range_begin": range_begin,
                            "care_range_end": range_end
                        },
                        "patient": {
                            "id": patient_row[0],
                            "name": patient_row[1],
                            "insurance_number": patient_row[2],
                            "birthdate": patient_row[3],
                            "care_level": patient_row[4]
                        },
                        "services": [
                            {
                                "id": s[0],
                                "code": s[1],
                                "description": s[2],
                                "quantity": s[3],
                                "unit_price": s[4],
                                "total_price": s[5]
                            } for s in services_rows
                        ]
                    }
                    cases.append(case)
        else:
            # Get all billable invoices for month
            c.execute("""
                SELECT ce.care_event_id, ce.patient_id, ce.invoicing_month, ce.invoice_number,
                       ce.amount_owed, ce.sum_covered, ce.sum_total, ce.care_range_begin, ce.care_range_end
                FROM care_events ce
                JOIN billing_details bd ON ce.care_event_id = bd.care_event_id
                WHERE ce.invoicing_month = ? AND ce.event_type IN ('SGBXI', 'Entleistung')
                AND bd.payment_status = 'invoice_needed'
            """, (invoicing_month,))
            
            for row in c.fetchall():
                care_event_id, patient_id, month, inv_num, amt_owed, sum_cov, sum_tot, range_begin, range_end = row
                
                # Get patient
                c.execute("SELECT patient_id, patient_name, insurance_number, birthdate, care_level FROM patient_profiles WHERE patient_id = ?", (patient_id,))
                patient_row = c.fetchone()
                
                # Get services
                c.execute("SELECT service_id, code, description, quantity, unit_price, total_price FROM care_services_new WHERE care_event_id = ?", (care_event_id,))
                services_rows = c.fetchall()
                
                if patient_row:
                    case = {
                        "invoice": {
                            "id": care_event_id,
                            "invoicing_month": month,
                            "invoice_number": inv_num,
                            "amount_owed": amt_owed,
                            "sum_covered": sum_cov,
                            "sum_total": sum_tot,
                            "care_range_begin": range_begin,
                            "care_range_end": range_end
                        },
                        "patient": {
                            "id": patient_row[0],
                            "name": patient_row[1],
                            "insurance_number": patient_row[2],
                            "birthdate": patient_row[3],
                            "care_level": patient_row[4]
                        },
                        "services": [
                            {
                                "id": s[0],
                                "code": s[1],
                                "description": s[2],
                                "quantity": s[3],
                                "unit_price": s[4],
                                "total_price": s[5]
                            } for s in services_rows
                        ]
                    }
                    cases.append(case)
    
    return cases

def check_missing_patient_fields(invoicing_month, auto_fix=False):
    """
    Check for patients missing required fields (address data) for billing in month.
    Uses new unified schema: patient_profiles, care_events.
    
    Args:
        invoicing_month: Month to check (MMYYYY format)
        auto_fix: If True, automatically use placeholder values. If False, just log warnings.
    """
    with get_db() as conn:
        c = conn.cursor()

        c.execute("""
            SELECT DISTINCT p.patient_id, p.patient_name, p.insurance_number, 
                   p.street_name, p.street_number, p.postal_code, p.city
            FROM patient_profiles p
            JOIN care_events ce ON p.patient_id = ce.patient_id
            WHERE ce.invoicing_month = ? AND ce.event_type IN ('SGBXI', 'Entleistung')
        """, (invoicing_month,))

        patients = c.fetchall()

        for patient_id, name, insurance_number, street_name, street_number, postal, city in patients:
            if street_name and street_number and postal and city:
                continue

            logger.warning(f"Patient '{name}' ({insurance_number}) is missing address fields.")

            if auto_fix:
                # Use placeholders instead of prompting
                street_name = street_name or "[STRASSE ERFORDERLICH]"
                street_number = street_number or "[HAUSNUMMER ERFORDERLICH]"
                postal = postal or "[PLZ ERFORDERLICH]"
                city = city or "[ORT ERFORDERLICH]"
                
                c.execute("""
                    UPDATE patient_profiles SET street_name = ?, street_number = ?, postal_code = ?, city = ? WHERE patient_id = ?
                """, (street_name, street_number, postal, city, patient_id))
                logger.info(f"Patient {name}: auto-fixed with placeholder values.")
            else:
                logger.warning(f"  - Street Name: {'MISSING' if not street_name else 'OK'}")
                logger.warning(f"  - Street Number: {'MISSING' if not street_number else 'OK'}")
                logger.warning(f"  - Postal Code: {'MISSING' if not postal else 'OK'}")
                logger.warning(f"  - City: {'MISSING' if not city else 'OK'}")

        conn.commit()

def check_service_fields(auto_fix=False):
    """
    Check for services with quantity mismatches in unified schema.
    Uses care_services_new table.
    
    Args:
        auto_fix: If True, automatically fix mismatches. If False, just log warnings.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
        SELECT service_id, care_event_id, quantity, code, description, unit_price, total_price
        FROM care_services_new
        """)

        rows = cursor.fetchall()
        
        for row in rows:
            service_id, care_event_id, quantity, service_code, description, unit_price, total_price = row
            if not quantity or not unit_price or not total_price:
                continue
                
            try:
                unit_price_float = float(str(unit_price).replace(",", "."))
                total_price_float = float(str(total_price).replace(",", "."))
                quantity_float = float(str(quantity).replace(",", "."))
            except ValueError:
                logger.warning(f"Service {service_id}: non-numeric values, skipping check")
                continue

            if unit_price_float == 0:
                continue

            calculated_quantity = total_price_float / unit_price_float
            percentage_diff = abs(calculated_quantity - quantity_float) / abs(calculated_quantity) * 100

            if percentage_diff < 0.5:
                continue  # close enough

            logger.warning(f"Service {service_id}: quantity mismatch (expected {calculated_quantity:.2f}, got {quantity_float}).")
            
            if auto_fix:
                correct_quantity_str = f"{calculated_quantity:.2f}"
                cursor.execute("""
                    UPDATE care_services_new SET quantity = ? WHERE service_id = ?
                """, (correct_quantity_str, service_id))
                logger.info(f"Service {service_id}: auto-fixed quantity to {correct_quantity_str}.")
            else:
                logger.warning(f"  - Care Event: {care_event_id}")
                logger.warning(f"  - Service Code: {service_code}")
                logger.warning(f"  - Expected Quantity: {calculated_quantity:.2f}")
                logger.warning(f"  - Current Quantity: {quantity_float}")

        conn.commit()

def mark_month_ready_for_generation(invoicing_month: str, only_positive: bool = False) -> int:
    """
    Create billing_details rows for care_events that need invoicing.
    
    Only creates billing_details for care_events that need invoicing.
    
    SGBXI Logic:
    - Always create billing_details (to charge investitionskosten to all)
    - payment_status = 'invoice_needed'
    
    Entleistung Logic:
    - Track cumulative usage per patient per calendar year
    - Yearly limit: 125 EUR × 12 = 1,500 EUR per year
    - If cumulative ≤ 1,500 EUR → NO billing_details created (covered by insurance)
    - If cumulative > 1,500 EUR → billing_details created with payment_status = 'invoice_needed'
    
    SGBV/Verhinderungspflege/Consultation:
    - Never create billing_details (non-billable)
    
    invoicing_month format: MMYYYY (e.g., "012025" for January 2025)
    
    Returns number of billing_details rows created.
    """
    from app.utils.parsing import generate_id
    from datetime import datetime
    
    with get_db() as conn:
        c = conn.cursor()
        before = conn.total_changes

        # Get all billable care_events for this invoicing month that don't have billing_details yet
        c.execute("""
            SELECT ce.care_event_id, ce.patient_id, ce.event_type
            FROM care_events ce
            WHERE ce.event_type IN ('SGBXI', 'Entleistung')
            AND ce.invoicing_month = ?
            AND NOT EXISTS (
                SELECT 1 FROM billing_details bd 
                WHERE bd.care_event_id = ce.care_event_id
            )
        """, (invoicing_month,))
        
        care_events_to_process = c.fetchall()
        billing_rows_created = 0
        
        for care_event_id, patient_id, event_type in care_events_to_process:
            
            # Get invoice-level data from care_events (stored as REAL, no parsing needed)
            c.execute("""
                SELECT sum_covered, sum_total FROM care_events
                WHERE care_event_id = ?
            """, (care_event_id,))
            
            event_row = c.fetchone()
            if not event_row:
                logger.error(f"Care event {care_event_id} not found")
                continue
                
            sum_covered = event_row[0] if event_row[0] else 0.0
            sum_total = event_row[1] if event_row[1] else 0.0
            
            # Calculate investitionskosten: 6% of sum_total ONLY for SGBXI
            investitionskosten = 0.0
            if event_type == "SGBXI":
                investitionskosten = sum_total * 0.06
            
            # amount_owed = sum_total - sum_covered + investitionskosten (all numeric, no parsing)
            amount_owed = sum_total - sum_covered + investitionskosten
            
            # Determine if invoice should be created
            should_create_billing = False
            billing_status = "covered_insurance"
            
            if event_type == "SGBXI":
                # SGBXI: Always create billing_details
                should_create_billing = True
                billing_status = "invoice_needed"
            
            elif event_type == "Entleistung":
                # Entleistung: Check cumulative usage for the year
                year = int(invoicing_month[-4:])
                
                # Get current cumulative amount for this patient in this year
                c.execute("""
                    SELECT cumulative_amount FROM entlastungsleistung_tracking
                    WHERE patient_id = ? AND calendar_year = ?
                """, (patient_id, year))
                
                tracking_row = c.fetchone()
                cumulative = float(tracking_row[0]) if tracking_row and tracking_row[0] else 0.0
                
                new_cumulative = cumulative + sum_total
                
                # Check against yearly limit: 125 EUR × 12 = 1,500 EUR
                YEARLY_LIMIT = 1500.0
                
                if new_cumulative > YEARLY_LIMIT:
                    # Exceeds limit → create billing_details
                    should_create_billing = True
                    billing_status = "invoice_needed"
                else:
                    # Within limit → NO billing_details (covered by insurance)
                    should_create_billing = False
                
                # Update cumulative tracking
                tracking_id = generate_id("trk")
                c.execute("""
                    INSERT INTO entlastungsleistung_tracking 
                    (tracking_id, org_id, patient_id, calendar_year, cumulative_amount, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(org_id, patient_id, calendar_year) DO UPDATE SET
                        cumulative_amount = excluded.cumulative_amount,
                        last_updated = CURRENT_TIMESTAMP
                """, (
                    tracking_id,
                    'org_default',
                    patient_id,
                    year,
                    new_cumulative,
                    datetime.now().isoformat()
                ))
            
            # Create billing_details ONLY if invoice is needed
            if should_create_billing:
                billing_id = generate_id("bill")
                c.execute("""
                    INSERT INTO billing_details 
                    (billing_detail_id, org_id, care_event_id, invoicing_month, 
                     sum_covered, sum_total, investitionskosten, amount_owed, invoice_number, billing_status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    billing_id,
                    'org_default',
                    care_event_id,
                    invoicing_month,
                    sum_covered,
                    sum_total,
                    investitionskosten,
                    amount_owed,
                    None,
                    billing_status,
                    datetime.now().isoformat()
                ))
                billing_rows_created += 1

        conn.commit()
        changes = conn.total_changes - before
        logger.info(f"Created {billing_rows_created} billing_details rows in {invoicing_month}: SGBXI=all, Entleistung=only if exceeds 1500 EUR limit")
        return billing_rows_created
