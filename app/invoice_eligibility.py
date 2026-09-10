from fastapi import HTTPException


AWAITING_RECONCILIATION = "awaiting_reconciliation"
CONFIRMED_ENTLASTUNG_STATUSES = {
    "confirmed_limit_partial",
    "confirmed_limit_full",
}


def assert_invoice_eligible(billing_detail, care_event) -> None:
    """Reject billing rows that are not eligible for private invoice issuance.

    Entlastungsleistung private invoices always require an RZH-confirmed
    settlement; internal allowance calculations are reference-only.
    """
    status = billing_detail.get("billing_status")
    if status == AWAITING_RECONCILIATION:
        raise HTTPException(409, "Entlastungsleistung is awaiting RZH reconciliation")
    if status == "covered_insurance":
        raise HTTPException(409, "Fully covered services do not require a private invoice")
    if status == "not_needed":
        raise HTTPException(409, "This service does not require a private invoice")
    if status != "invoice_needed":
        raise HTTPException(409, "Billing detail is not eligible for invoice issuance")
    if care_event.get("event_type") == "Entleistung" and billing_detail.get("reconciliation_status") not in CONFIRMED_ENTLASTUNG_STATUSES:
        raise HTTPException(409, "Entlastungsleistung requires confirmed RZH settlement before invoicing")
