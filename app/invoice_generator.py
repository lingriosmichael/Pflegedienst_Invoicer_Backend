import os
import logging
import re
import unicodedata
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pathlib import Path
from tempfile import NamedTemporaryFile
from app.db import DEFAULT_ORG_ID
from app.db.mongodb_config import get_database
from app.invoice_issuance import ensure_service_packets, prepare_invoice
from weasyprint import HTML
from datetime import datetime
from app.database import get_private_invoice_cases
# Use MongoDB repositories
from app.db.mongodb_repositories import InvoiceRepository
from collections import defaultdict
import signal

logger = logging.getLogger(__name__)

# Disable HarfBuzz assertions to prevent crashes on macOS
os.environ['HARFBUZZ_DEBUG'] = '0'

TEMPLATE_DIR = "templates"
OUTPUT_DIR = "output/invoices"
os.makedirs(OUTPUT_DIR, exist_ok=True)

env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape(["html", "xml"]))


def deny_resource_fetch(url, *args, **kwargs):
    raise ValueError("External PDF resources are disabled")


def invoice_filename_component(value):
    """Return a readable, filesystem-safe patient-name component."""
    normalized = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    component = re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_")
    return component[:120] or "Patient"

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

    patient_name = invoice_filename_component(data["patient"].get("name"))
    output_path = Path(OUTPUT_DIR) / f"RE_{data['invoice']['invoice_number']}_{patient_name}.pdf"
    temporary = None
    try:
        with NamedTemporaryFile(dir=OUTPUT_DIR, suffix=".pdf", delete=False) as stream:
            temporary = Path(stream.name)
        HTML(string=html_content, url_fetcher=deny_resource_fetch).write_pdf(str(temporary))
        temporary.chmod(0o600)
        temporary.replace(output_path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return str(output_path)


def generate_billing_pdf(billing_detail_id):
    from datetime import timezone
    case = prepare_invoice(billing_detail_id)
    path = generate_invoice_pdf(case)
    result = get_database().billing_details.update_one(
        {"org_id": DEFAULT_ORG_ID, "billing_detail_id": billing_detail_id, "generation_id": case["generation_id"]},
        {"$set": {"pdf_path": Path(path).name, "invoice_created_date": datetime.now(timezone.utc)}})
    if not result.matched_count:
        raise ValueError("Invoice changed while rendering; regenerate its current version")
    return path


def process_generate_invoices(invoicing_month=None, include_orphaned=True, require_invoice_needed=True,
                              billing_detail_ids=None, allowed_event_types=None):
    from app.utils.validation import validate_month
    validate_month(invoicing_month)
    if include_orphaned:
        ensure_service_packets(invoicing_month)
    eligible_statuses = ["invoice_needed"] if require_invoice_needed else ["invoice_needed", "sent", "paid"]
    query = {"org_id": DEFAULT_ORG_ID, "invoicing_month": invoicing_month,
             "billing_status": {"$in": eligible_statuses}}
    if billing_detail_ids is not None:
        query["billing_detail_id"] = {"$in": list(billing_detail_ids)}
    bills = list(get_database().billing_details.find(query))
    if allowed_event_types is not None:
        event_by_id = {
            event["care_event_id"]: event.get("event_type")
            for event in get_database().care_events.find(
                {"org_id": DEFAULT_ORG_ID,
                 "care_event_id": {"$in": [bill["care_event_id"] for bill in bills]}},
                {"care_event_id": 1, "event_type": 1},
            )
        }
        bills = [bill for bill in bills if event_by_id.get(bill["care_event_id"]) in allowed_event_types]
    summary = {"total_cases": len(bills), "generated": 0, "failed": 0, "failed_invoice_ids": []}
    for bill in bills:
        try:
            generate_billing_pdf(bill["billing_detail_id"])
            summary["generated"] += 1
        except Exception:
            logger.warning("PDF generation failed for billing detail %s", bill["billing_detail_id"])
            summary["failed"] += 1
            summary["failed_invoice_ids"].append(bill["billing_detail_id"])
    return summary
