import os
import sqlite3
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML
from datetime import datetime
from app.database import get_private_invoice_cases

TEMPLATE_DIR = "templates"
OUTPUT_DIR = "output/invoices"
os.makedirs(OUTPUT_DIR, exist_ok=True)

env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))

# ✅ Register the filter BEFORE rendering
def split_address(address):
    if not address:
        return "", ""
    parts = address.split()
    for i, part in enumerate(parts):
        if part.isdigit() and len(part) == 5:  
            return " ".join(parts[:i]), " ".join(parts[i:])
    return address, ""

env.filters['split_address'] = split_address

def generate_invoice_pdf(data):    
    care_account = data["invoice"].get("care_account")
    if care_account == "4064":
        template_name = "invoice_template_4064.html"
    else:
        template_name = "invoice_template.html"

    template = env.get_template(template_name)

    html_content = template.render(
        patient=data["patient"],
        invoice=data["invoice"],
        services=data["services"],
        today=datetime.now().strftime("%d.%m.%Y"),
        invoice_number=data["invoice"]["invoice_number"]
    )
    raw_name = data['patient']['name']
    formatted_name = raw_name.replace(" ", "").replace(",", "_")
    output_path = os.path.join(OUTPUT_DIR, f"RE_{data['invoice']['invoice_number']}_{formatted_name}.pdf")
    HTML(string=html_content).write_pdf(output_path)

    return output_path

def process_generate_invoices(invoicing_month=None):
    conn = sqlite3.connect("data/invoices.db")
    c = conn.cursor()

    # Get the latest invoice_number to start incrementing from
    c.execute("SELECT MAX(CAST(invoice_number AS INTEGER)) FROM invoices WHERE invoice_number IS NOT NULL")
    max_invoice_number = c.fetchone()[0] or 4006907
    next_invoice_number = max_invoice_number + 1

    # Fetch cases filtered by invoicing_month
    cases = get_private_invoice_cases(invoicing_month)

    for case in cases:
        try:
            current_number = case["invoice"].get("invoice_number")
            invoice_id = case["invoice"].get("invoice_id")  # ensure this exists in your grouped result

            # Assign invoice number if missing
            if not current_number:
                assigned_number = next_invoice_number
                next_invoice_number += 1
                case["invoice"]["invoice_number"] = assigned_number

                # Update in DB
                c.execute("UPDATE invoices SET invoice_number = ? WHERE id = ?", (
                    assigned_number,
                    invoice_id
                ))
                conn.commit()

            path = generate_invoice_pdf(case)
            print(f"✅ PDF created: {path}")

        except Exception as e:
            invoice_number = case["invoice"].get("invoice_number", "[unknown]")
            print(f"❌ PDF failed for invoice {invoice_number}: {e}")

    conn.close()

def regenerate_invoice(invoice_number):
    conn = sqlite3.connect("data/invoices.db")
    c = conn.cursor()

    # Fetch invoice_id for the given invoice_number
    c.execute("SELECT id FROM invoices WHERE invoice_number = ?", (invoice_number,))
    result = c.fetchone()

    if not result:
        print(f"❌ No invoice found with number {invoice_number}.")
        conn.close()
        return

    invoice_id = result[0]
    cases = get_private_invoice_cases(invoice_id=invoice_id)

    if not cases:
        print(f"❌ No data found for invoice {invoice_number}.")
        conn.close()
        return

    # There should be only one case
    case = cases[0]

    try:
        path = generate_invoice_pdf(case)
        print(f"✅ PDF regenerated: {path}")
    except Exception as e:
        print(f"❌ PDF regeneration failed for invoice {invoice_number}: {e}")

    conn.close()