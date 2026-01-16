import os
import logging
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML
from datetime import datetime
from app.database import get_private_invoice_cases
# Future: from app.db.repositories import InvoiceRepository, PatientRepository, ServiceRepository
from collections import defaultdict
import signal

logger = logging.getLogger(__name__)

# Disable HarfBuzz assertions to prevent crashes on macOS
os.environ['HARFBUZZ_DEBUG'] = '0'

TEMPLATE_DIR = "templates"
OUTPUT_DIR = "output/invoices"
os.makedirs(OUTPUT_DIR, exist_ok=True)

env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))

def split_address(address):
    if not address:
        return "", ""
    parts = address.split()
    for i, part in enumerate(parts):
        if part.isdigit() and len(part) == 5:  
            return " ".join(parts[:i]), " ".join(parts[i:])
    return address, ""

def format_german_number(value):
    """Format a number in German locale: 1234.56 -> 1.234,56"""
    if value is None or value == "":
        return "0,00"
    
    try:
        # Convert to float if string
        if isinstance(value, str):
            num = float(value.replace(",", "."))
        else:
            num = float(value)
        
        # Format with 2 decimal places
        formatted = f"{num:,.2f}"
        # Replace periods with a placeholder to avoid confusion
        formatted = formatted.replace(",", "#").replace(".", ",").replace("#", ".")
        return formatted
    except (ValueError, TypeError):
        return str(value)

env.filters['split_address'] = split_address
env.filters['german_number'] = format_german_number

def parse_price(value):
    """Convert string prices like '32,00 EUR' to float."""
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value).replace("EUR", "").replace("€", "").replace(",", ".").strip())

def group_services(services):
    grouped = defaultdict(lambda: {
        "code": "",
        "description": "",
        "unit_price": "", 
        "quantity": 0.0,
        "total_price": 0.0
    })

    for service in services:
        try:
            code = service["code"]
            description = service["description"].strip()
            quantity = float(str(service["quantity"]).replace(",", "."))
            
            # Handle missing or empty prices - set to 0
            unit_price_str = service.get("unit_price", "0") or "0"
            total_price_str = service.get("total_price", "0") or "0"
            
            unit_price_float = parse_price(unit_price_str) if unit_price_str else 0.0
            total_price_float = parse_price(total_price_str) if total_price_str else 0.0

            key = (code, description, f"{unit_price_float:.2f}")

            grouped_service = grouped[key]
            grouped_service["code"] = code
            grouped_service["description"] = description
            grouped_service["unit_price"] = f"{unit_price_float:.2f}".replace(".", ",") if unit_price_float > 0 else ""
            grouped_service["quantity"] += quantity
            grouped_service["total_price"] += total_price_float

        except Exception as e:
            logger.warning(f"Error processing service: {service} — {e}")

    return [{
        "code": v["code"],
        "description": v["description"],
        "quantity": str(int(v["quantity"])) if v["quantity"].is_integer() else f"{v['quantity']:.2f}",
        "unit_price": v["unit_price"],
        "total_price": f"{v['total_price']:.2f}".replace(".", ",") if v["total_price"] > 0 else ""
    } for v in grouped.values()]

def generate_invoice_pdf(data):    
    care_account = data["invoice"].get("care_account")
    event_type = data["invoice"].get("event_type")
    
    # Select template based on event type
    if event_type == "ServicePacket":
        template_name = "invoice_template_service_packet.html"
    elif event_type == "Entleistung":
        template_name = "invoice_template_4064.html"
    else:
        template_name = "invoice_template.html"

    data["services"] = group_services(data["services"])

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
    
    try:
        HTML(string=html_content).write_pdf(output_path)
    except SystemExit:
        # HarfBuzz can cause SystemExit on certain font operations
        # Try again with a simpler approach
        logger.warning(f"PDF generation failed with SystemExit, retrying for {output_path}")
        try:
            HTML(string=html_content).write_pdf(output_path)
        except Exception as e:
            logger.error(f"Failed to generate PDF on retry: {e}")
            raise

    return output_path

def process_generate_invoices(invoicing_month=None):
    from app.db.repositories import InvoiceRepository
    from app.database import get_orphaned_service_packet_cases
    
    cases = get_private_invoice_cases(invoicing_month)
    
    # After regular invoices, also generate invoices for orphaned service packets
    if invoicing_month:
        orphaned_cases = get_orphaned_service_packet_cases(invoicing_month)
        cases.extend(orphaned_cases)

    for case in cases:
        try:
            current_number = case["invoice"].get("invoice_number")
            invoice_id = case["invoice"].get("invoice_id")

            # Assign invoice number atomically if missing
            if not current_number:
                assigned_number = InvoiceRepository.get_next_invoice_number()
                case["invoice"]["invoice_number"] = assigned_number
                # Update in DB using repository (only if this is a real care event, not synthetic)
                if not case["invoice"]["id"].startswith("service_packet_"):
                    InvoiceRepository.update_invoice_number(invoice_id, assigned_number)

            path = generate_invoice_pdf(case)
            logger.info(f"PDF created: {path}")

        except Exception as e:
            invoice_number = case["invoice"].get("invoice_number", "[unknown]")
            logger.error(f"PDF failed for invoice {invoice_number}: {e}")


