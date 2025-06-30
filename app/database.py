import sqlite3
from datetime import datetime

DB_PATH = "data/invoices.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

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
        private_rechnung BOOLEAN DEFAULT 0,
        FOREIGN KEY(patient_id) REFERENCES patients(id)
    )
    """)

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

    conn.commit()
    conn.close()

def parse_decimal(val):
    if isinstance(val, (int, float)):
        return float(val)
    val = val.strip()

    if '.' in val and ',' in val:
        val = val.replace('.', '').replace(',', '.')
    elif ',' in val:
        val = val.replace(',', '.')

    try:
        return float(val)
    except ValueError:
        return 0.0

def insert_structured_data(data):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    patient = data["patient"]
    invoice = data["invoice"]
    services = data["services"]

    if "care_account" not in invoice:
        invoice["care_account"] = patient.get("pflege_konto", "")

    invoice["summe_covered"] = f"{parse_decimal(invoice['summe_covered']):.2f}"
    invoice["summe_total"] = f"{parse_decimal(invoice['summe_total']):.2f}"
    invoice["amount_owed"] = f"{parse_decimal(invoice['summe_total']) - parse_decimal(invoice['summe_covered']):.2f}"

    try:
        total_val = float(invoice["summe_total"])
        covered_val = float(invoice["summe_covered"])
    except ValueError:
        print(f"❌ Invalid invoice totals — skipping.")
        conn.close()
        return

    amount_owed = total_val - covered_val
    invoice["amount_owed"] = f"{amount_owed:,.2f}".replace('.', ',').replace(',', '.', 1)

    for s in services:
        s["quantity"] = s["quantity"].replace(',', '.')
        s["unit_price"] = parse_decimal(s["unit_price"])
        s["total_price"] = parse_decimal(s["total_price"])

    c.execute("""
        INSERT OR IGNORE INTO patients (name, birthdate, insurance_number, care_level)
        VALUES (?, ?, ?, ?)
    """, (
        patient["name"],
        patient["birthdate"],
        patient["insurance_number"],
        patient["care_level"]
    ))

    c.execute("SELECT id FROM patients WHERE insurance_number = ?", (patient["insurance_number"],))
    row = c.fetchone()
    if not row:
        print(f"❌ Patient not found after insert — skipping.")
        conn.close()
        return
    patient_id = row[0]

    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    private_flag = int(data.get("private_rechnung", False))

    c.execute("""
        INSERT INTO invoices (
            patient_id, care_range_begin, care_range_end,
            sum_covered, sum_total, amount_owed, created_at,
            care_account, invoicing_month, private_rechnung
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
        private_flag
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
    conn.close()

def get_private_invoice_cases(invoicing_month=None, invoice_id=None):
    conn = sqlite3.connect("data/invoices.db")
    c = conn.cursor()

    if invoice_id:
        c.execute("""
            SELECT invoices.id, invoices.invoice_number, invoices.patient_id,
                   invoices.sum_covered, invoices.sum_total, invoices.amount_owed,
                   invoices.invoicing_month, invoices.care_account,
                   patients.name, patients.address, patients.debtor_number,
                   patients.birthdate, patients.insurance_number, patients.care_level
            FROM invoices
            JOIN patients ON invoices.patient_id = patients.id
            WHERE invoices.id = ? AND invoices.private_rechnung = 1
        """, (invoice_id,))
    else:
        c.execute("""
            SELECT invoices.id, invoices.invoice_number, invoices.patient_id,
                   invoices.sum_covered, invoices.sum_total, invoices.amount_owed,
                   invoices.invoicing_month, invoices.care_account,
                   patients.name, patients.address, patients.debtor_number,
                   patients.birthdate, patients.insurance_number, patients.care_level
            FROM invoices
            JOIN patients ON invoices.patient_id = patients.id
            WHERE invoices.private_rechnung = 1
              AND (invoices.invoicing_month = ? OR ? IS NULL)
        """, (invoicing_month, invoicing_month))

    invoice_rows = c.fetchall()

    cases = []
    for row in invoice_rows:
        (
            invoice_id, invoice_number, patient_id, sum_covered, sum_total, amount_owed,
            invoicing_month, care_account, name, address, debtor_number,
            birthdate, insurance_number, care_level
        ) = row

        c.execute("""
            SELECT quantity, code, description, unit_price, total_price
            FROM services
            WHERE invoice_id = ?
        """, (invoice_id,))
        service_rows = c.fetchall()

        services = []
        for s in service_rows:
            services.append({
                "quantity": s[0],
                "code": s[1],
                "description": s[2],
                "unit_price": s[3],
                "total_price": s[4],
            })

        case = {
            "invoice": {
                "invoice_id": invoice_id,
                "invoice_number": invoice_number,
                "summe_covered": sum_covered,
                "summe_total": sum_total,
                "betrag_offen": amount_owed,
                "abrechnungsmonat": invoicing_month,
                "care_account": care_account,
            },
            "patient": {
                "name": name,
                "address": address,
                "debtor_number": debtor_number,
                "birthdate": birthdate,
                "insurance_number": insurance_number,
                "care_level": care_level,
            },
            "services": services
        }

        cases.append(case)

    conn.close()
    return cases

def check_missing_patient_fields(invoicing_month):
    conn = sqlite3.connect(DB_PATH)
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

        print(f"\n⚠️  Patient '{name}' ({insurance_number}) is missing:")

        if not address:
            address = input("📬 Enter address: ").strip()

        if not debtor:
            debtor = input("🧾 Enter debtor number: ").strip()

        c.execute("""
            UPDATE patients SET address = ?, debtor_number = ? WHERE id = ?
        """, (address, debtor, patient_id))
        print("✅ Updated.\n")

    conn.commit()
    conn.close()

def check_service_fields():
    conn = sqlite3.connect(DB_PATH)
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

        print(f"\n❌ Mistake found in row ID {service_id}:")
        print(f"invoice_id={invoice_id}, service_code={service_code}, description={description}")
        print(f"unit_price={unit_price}, total_price={total_price}, quantity={quantity}")
        print(f"Expected quantity: {calculated_quantity}")
        correct_quantity_str = str(calculated_quantity)

        fix = input("🔧 Do you want to fix it? (Yes/No): ").strip().lower()

        if fix == "yes":
            print(f"Recommended: {correct_quantity_str}")
            new_quantity = input("New quantity to set (enter as string): ").strip()

            cursor.execute("""
                UPDATE services
                SET quantity = ?
                WHERE id = ?
            """, (new_quantity, service_id))
        
            conn.commit()
            print(f"✅ Row {service_id} updated to quantity={new_quantity}.")
        else:
            print(f"⏭️ Skipping row {service_id}.")

    conn.close()
    print("✅ Check complete.")
