"""Durable, parse-version-independent RZH reconciliation primitives.

This first phase stores and classifies settlement evidence only. It deliberately
does not create patient invoices or alter Entlastungsleistung balances.
"""

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from app.db import DEFAULT_ORG_ID
from app.db.mongodb_config import get_database
from app.db.transactions import transactional
from app.invoice_eligibility import CONFIRMED_ENTLASTUNG_STATUSES, assert_invoice_eligible
from app.utils.parsing import generate_id
from app.utils.validation import parse_service_date
from fastapi import HTTPException


ENTITLEMENT_EXHAUSTED = "ENTITLEMENT_EXHAUSTED"
ENTITLEMENT_MONTHLY_MAX_EXCEEDED = "ENTITLEMENT_MONTHLY_MAX_EXCEEDED"
NONPAYMENT_COLLECTION = "NONPAYMENT_COLLECTION"
LATE_PAYMENT_CREDIT = "LATE_PAYMENT_CREDIT"
DIRECT_PAYMENT = "DIRECT_PAYMENT"
DUPLICATE_BILLING = "DUPLICATE_BILLING"
MISSING_AUTHORIZATION = "MISSING_AUTHORIZATION"
MEMBERSHIP_NOT_ACTIVE = "MEMBERSHIP_NOT_ACTIVE"
OTHER_MANUAL_REVIEW = "OTHER_MANUAL_REVIEW"

MANUALLY_CONFIRMABLE_REASONS = {
    ENTITLEMENT_EXHAUSTED,
    ENTITLEMENT_MONTHLY_MAX_EXCEEDED,
    NONPAYMENT_COLLECTION,
}


def billing_detail_already_issued(bill: dict | None) -> bool:
    """True if this billing row already has an invoice out (sent/paid/rendered).

    Single source of truth for the "issued invoice requires an explicit
    accounting correction" guard in confirm_private_amount, so the review list
    can surface the same fact up front instead of staff discovering it only
    after a failed confirm click.
    """
    if not bill:
        return False
    return bool(bill.get("billing_status") in {"sent", "paid"} or bill.get("invoice_number") or bill.get("pdf_path"))

SECTION_HEADINGS = {
    "absetzungen": "absetzung",
    "gutschriften": "gutschrift",
    "korrekturen mit honorar": "korrektur_mit_honorar",
    "korrekturen ohne honorar": "korrektur_ohne_honorar",
    "abgerechnete belege": None,
}


def parse_german_cents(value: str) -> int:
    """Parse a signed German currency value into integer cents."""
    cleaned = str(value).strip().replace("€", "").replace("EUR", "").strip()
    if not re.fullmatch(r"[-+]?(?:\d{1,3}(?:\.\d{3})*|\d+)(?:,\d{1,2})?", cleaned):
        raise ValueError("Invalid German currency value")
    try:
        decimal = Decimal(cleaned.replace(".", "").replace(",", "."))
    except InvalidOperation as error:
        raise ValueError("Invalid German currency value") from error
    return int((decimal * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _normalized(value: str | None) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    return "".join(char for char in value if not unicodedata.combining(char)).casefold()


def classify_reason(reason_text: str | None) -> str:
    text = _normalized(reason_text)
    if re.search(r"monatlich\w* hochstbetrag.*uberschritt", text):
        return ENTITLEMENT_MONTHLY_MAX_EXCEEDED
    if "hochstbetrag wurde ausgeschopft" in text:
        return ENTITLEMENT_EXHAUSTED
    if "trotz mahnung keine zahlung" in text or "zahlung nicht eingegangen" in text:
        return NONPAYMENT_COLLECTION
    if "nachtragl" in text and "zahlungseingang" in text:
        return LATE_PAYMENT_CREDIT
    if "direktzahlung" in text or "direkt bezahlt" in text:
        return DIRECT_PAYMENT
    if "doppel" in text and "abrechnung" in text:
        return DUPLICATE_BILLING
    if "genehmigung" in text or "verordnung fehlt" in text:
        return MISSING_AUTHORIZATION
    if "mitgliedschaft" in text or "nicht versichert" in text:
        return MEMBERSHIP_NOT_ACTIVE
    return OTHER_MANUAL_REVIEW


def settlement_item_fingerprint(item: dict) -> str:
    """Identify the patient-level settlement fact independently of parser runs."""
    identity = {
        "rzh_transaction_number": item.get("rzh_transaction_number"),
        "original_invoice_number": item.get("original_invoice_number"),
        "insurance_number": item.get("insurance_number"),
        "care_account": str(item.get("care_account") or ""),
        "service_period_start": str(item.get("service_period_start") or ""),
        "service_period_end": str(item.get("service_period_end") or ""),
        "amount_cents": item.get("amount_cents"),
        "section_type": item.get("section_type"),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _first_match(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None


def _last_match(pattern: str, text: str) -> str | None:
    matches = list(re.finditer(pattern, text, flags=re.IGNORECASE | re.MULTILINE))
    return matches[-1].group(1).strip() if matches else None


def _period(text: str) -> tuple[str | None, str | None]:
    match = re.search(r"(?:pflegezeitraum|leistungszeitraum)\s*:?\s*(\d{2}\.\d{2}\.\d{2,4})\s*(?:-|bis)\s*(\d{2}\.\d{2}\.\d{2,4})", text, flags=re.IGNORECASE)
    if not match:
        return None, None
    return match.group(1), match.group(2)


def _split_settlement_sections(text: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"^\s*(Absetzungen|Gutschriften|Korrekturen mit Honorar|Korrekturen ohne Honorar|Abgerechnete Belege)\s*$", text, flags=re.IGNORECASE | re.MULTILINE))
    sections = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        heading = _normalized(match.group(1))
        section_type = SECTION_HEADINGS[heading]
        if section_type:
            sections.append((section_type, text[match.end():end]))
    return sections


def parse_settlement_text(text: str) -> tuple[list[dict], list[str]]:
    """Conservatively extract complete, patient-level settlement observations.

    A block without a stable transaction/invoice reference, insurance number,
    4064 account, reason, and explicitly labelled adjustment amount is reported
    as incomplete rather than guessed.
    """
    parsed, warnings = [], []
    for section_type, section in _split_settlement_sections(text):
        blocks = list(re.finditer(r"(?=^\s*Verordnung\s*:)", section, flags=re.IGNORECASE | re.MULTILINE))
        for index, block in enumerate(blocks):
            start = block.start()
            end = blocks[index + 1].start() if index + 1 < len(blocks) else len(section)
            body = section[start:end]
            prior = section[:start]
            patient_name = _first_match(r"^\s*Verordnung\s*:\s*(.+)$", body)
            insurance_number = _first_match(r"(?:versicherten(?:nummer|nr\.?))\s*:?\s*([A-Z0-9-]+)", body)
            care_account = _first_match(r"pflegekonto\s*:?\s*(\d{4})", body)
            reason = _first_match(r"(?:begr[üu]ndung|abw\.?\s*grund)\s*:?\s*(.+)$", body)
            amount_text = _first_match(r"(?:absetzungsbetrag|gutschrift(?:sbetrag)?|korrekturbetrag)\s*:?\s*([-+]?[\d.]+(?:,\d{1,2})?)", body)
            transaction = _last_match(r"(?:vorgangsnummer|transaktionsnummer|belegnummer)\s*:?\s*([A-Z0-9-]+)", prior + "\n" + body)
            original_invoice = _last_match(r"(?:original(?:rechnungs)?nummer|rechnungsnummer|rechnung)\s*:?\s*([A-Z0-9-]+)", prior + "\n" + body)
            start_date, end_date = _period(body)
            if not all([insurance_number, care_account == "4064", reason, amount_text, transaction or original_invoice]):
                warnings.append("Incomplete patient-level settlement block requires manual review")
                continue
            try:
                amount_cents = parse_german_cents(amount_text)
            except ValueError:
                warnings.append("Invalid patient-level settlement amount requires manual review")
                continue
            if amount_cents == 0:
                warnings.append("Zero-value settlement block requires manual review")
                continue
            parsed.append({
                "section_type": section_type,
                "rzh_transaction_number": transaction,
                "original_invoice_number": original_invoice,
                "patient_name_raw": patient_name,
                "insurance_number": insurance_number,
                "care_account": care_account,
                "service_period_start": start_date,
                "service_period_end": end_date,
                "amount_cents": amount_cents,
                "reason_raw": reason,
                "reason_code": classify_reason(reason),
            })
    if parsed:
        return parsed, warnings
    for section_type, section in _split_settlement_sections(text):
        rows = list(re.finditer(r"(?=^\d{2}\.\d{2}\.\d{4}\s*$)", section, flags=re.MULTILINE))
        for index, row in enumerate(rows):
            body = section[row.start(): rows[index + 1].start() if index + 1 < len(rows) else len(section)]
            transaction = _first_match(r"^([A-Z]{3}\d+)\s*$", body)
            original_invoice = _first_match(r"\b(\d{6}-\d{8,12})\b", body)
            reason = _first_match(r"Begr[üu]ndung\s*\n\s*(.+)$", body)
            care_account = _first_match(r"Pflegekonto\s*:\s*(\d{4})", body)
            start_date, end_date = _period(body)
            amount_text = _first_match(r"\n\s*(-?[\d.]+,\d{2})\s*\n\s*Begr[üu]ndung", body)
            if not all([transaction, original_invoice, reason, care_account == "4064", amount_text, start_date, end_date]):
                warnings.append("Incomplete RZH table row requires manual review")
                continue
            parsed.append({"section_type": section_type, "rzh_transaction_number": transaction,
                "original_invoice_number": original_invoice, "insurance_number": None, "care_account": care_account,
                "service_period_start": start_date, "service_period_end": end_date,
                "amount_cents": parse_german_cents(amount_text), "reason_raw": reason,
                "reason_code": classify_reason(reason)})
    return parsed, warnings


_HEADER_ROW_RE = re.compile(
    r"^(?P<date>\d{2}\.\d{2}\.\d{4})\s+(?P<transaction>\S+)\s+(?P<rest>.+?)\s+(?P<amount>-?[\d.]+,\d{2})\s*$"
)

_VERORDNUNG_ROW_RE = re.compile(
    r"^Verordnung\s*:?\s*(?P<name>.+?)\s*-\s*(?P<insurance>[A-Z]\d{9})\s*-\s*Status:\s*\d+"
    r"(?:\s*-\s*Pflegegrad:\s*\d+)?\s+(?P<amount>-?[\d.]+,\d{2})\s*$",
    flags=re.IGNORECASE,
)

_VERORDNUNGSDATUM_ROW_RE = re.compile(
    r"pflegezeitraum\s*:?\s*(?P<start>\d{2}\.\d{2}\.\d{2,4})(?:\s*-\s*(?P<end>\d{2}\.\d{2}\.\d{2,4}))?"
    r".*?pflegekonto\s*:?\s*(?P<account>\d{4})?\s*$",
    flags=re.IGNORECASE,
)

_BARE_ACCOUNT_ROW_RE = re.compile(r"^\d{4}$")


def _page_rows(page) -> list[str]:
    """Group a page's words into visual table rows, ordered top-to-bottom, left-to-right."""
    lines = {}
    for x0, y0, _, _, word, *_ in page.get_text("words"):
        lines.setdefault(round(y0 / 3) * 3, []).append((x0, word))
    return [" ".join(word for _, word in sorted(line)) for _, line in sorted(lines.items())]


def _finalize_settlement_block(block: dict | None, parsed: list[dict], warnings: list[str]) -> None:
    """Emit only 4064 patient lines whose amounts exactly reconcile to the header total.

    A grouped RZH transaction can cover several patients under one header amount.
    We never allocate that total by guessing: either every child line parsed
    completely and their amounts sum exactly to the header amount, or the whole
    block is reported as requiring manual review and nothing is emitted.
    """
    if not block:
        return
    children = block["children"]
    if not children or not block.get("reason") or any(child.get("incomplete") for child in children):
        warnings.append("Incomplete RZH table row requires manual review")
        return
    if sum(child["amount_cents"] for child in children) != block["header_amount_cents"]:
        warnings.append("Grouped RZH transaction amount does not reconcile to patient-level lines")
        return
    for child in children:
        if child["care_account"] != "4064":
            continue
        parsed.append({
            "section_type": block["section_type"],
            "rzh_transaction_number": block["transaction"],
            "original_invoice_number": block["invoice"],
            "patient_name_raw": child["name"],
            "insurance_number": child["insurance"],
            "care_account": child["care_account"],
            "service_period_start": child["period_start"],
            "service_period_end": child["period_end"],
            "amount_cents": child["amount_cents"],
            "reason_raw": block["reason"],
            "reason_code": classify_reason(block["reason"]),
        })


def parse_settlement_pdf(path: str | Path) -> tuple[list[dict], list[str]]:
    """Extract RZH detail-table rows using word coordinates, not text order.

    A section (Absetzungen/Gutschriften/Korrekturen ...) commonly spans a page
    break without repeating its heading or the pending transaction header, so
    state is carried across the whole document rather than reset per page.
    """
    import fitz

    parsed: list[dict] = []
    warnings: list[str] = []
    document = fitz.open(path)
    rows: list[str] = []
    for page in document:
        rows.extend(_page_rows(page))

    section_type = None
    block: dict | None = None
    index = 0
    while index < len(rows):
        line = rows[index]
        normalized = _normalized(line)
        heading = SECTION_HEADINGS.get(normalized, "__not_a_heading__")
        if heading != "__not_a_heading__":
            _finalize_settlement_block(block, parsed, warnings)
            block = None
            section_type = heading
            index += 1
            continue
        if section_type is None:
            index += 1
            continue

        header = _HEADER_ROW_RE.match(line)
        if header:
            _finalize_settlement_block(block, parsed, warnings)
            # The last token of the carrier/invoice column is normally the
            # "Urspr. Rechnung" reference (always contains a digit in every
            # observed layout). If that column is ever blank, the last word
            # of the carrier name (e.g. "Sachsen") would land here instead --
            # reject anything with no digit rather than emit a bogus reference.
            trailing_token = header.group("rest").split()[-1]
            invoice = trailing_token if any(char.isdigit() for char in trailing_token) else None
            block = {"section_type": section_type, "transaction": header.group("transaction"),
                      "invoice": invoice,
                      "header_amount_cents": parse_german_cents(header.group("amount")),
                      "reason": None, "children": []}
            index += 1
            continue

        if block is not None and normalized.startswith("begrundung"):
            block["reason"] = re.sub(r"^Begr[üu]ndung\s*:?\s*", "", line, flags=re.IGNORECASE).strip()
            index += 1
            continue

        if block is not None:
            child = _VERORDNUNG_ROW_RE.match(line)
            if child:
                try:
                    amount_cents = parse_german_cents(child.group("amount"))
                except ValueError:
                    amount_cents = None
                block["children"].append({
                    "name": child.group("name").strip(), "insurance": child.group("insurance"),
                    "amount_cents": amount_cents, "period_start": None, "period_end": None,
                    "care_account": None, "incomplete": amount_cents is None,
                })
                index += 1
                continue

            if block["children"] and "pflegezeitraum" in normalized:
                lookahead = line
                if _BARE_ACCOUNT_ROW_RE.match(rows[index + 1].strip()) if index + 1 < len(rows) else False:
                    lookahead = f"{line} {rows[index + 1].strip()}"
                    index += 1
                period = _VERORDNUNGSDATUM_ROW_RE.search(lookahead)
                current_child = block["children"][-1]
                if period and period.group("account"):
                    current_child["period_start"] = period.group("start")
                    current_child["period_end"] = period.group("end") or period.group("start")
                    current_child["care_account"] = period.group("account")
                else:
                    current_child["incomplete"] = True
                index += 1
                continue

        index += 1

    _finalize_settlement_block(block, parsed, warnings)
    return parsed, warnings


def _overlaps_care_period(event: dict, start: str | None, end: str | None) -> bool:
    """True if the RZH line's service period falls in the same calendar month as the
    care event's. Every Entlastungsleistung 4064 claim is billed for a single
    calendar month, so same-month is the correct (and tighter) match key --
    any-date-overlap was too permissive and could in principle span two
    different months' claims for the same patient."""
    if not start or not end:
        return False
    try:
        reconciliation_start = parse_service_date(start).date()
        event_start = parse_service_date(event.get("period_start_date")).date()
    except (TypeError, ValueError):
        return False
    return (reconciliation_start.year, reconciliation_start.month) == (event_start.year, event_start.month)


def match_item(item: dict, database=None) -> dict:
    """Return an exact, ambiguous, or unmatched care-event association."""
    database = database if database is not None else get_database()
    if item.get("original_invoice_number") and not item.get("insurance_number"):
        candidates = list(database.care_events.find({"org_id": DEFAULT_ORG_ID, "event_type": "Entleistung",
            "care_account": "4064", "source_invoice_number": item["original_invoice_number"]}))
        identifiers = [candidate["care_event_id"] for candidate in candidates]
        if len(candidates) == 1:
            return {"match_status": "matched", "match_method": "source_invoice", "patient_id": candidates[0]["patient_id"],
                    "matched_care_event_id": identifiers[0], "candidate_care_event_ids": identifiers}
        return {"match_status": "ambiguous" if candidates else "unmatched", "match_method": None, "candidate_care_event_ids": identifiers}
    profile = database.patient_profiles.find_one({"org_id": DEFAULT_ORG_ID, "insurance_number": item["insurance_number"]})
    if not profile:
        return {"match_status": "unmatched", "match_method": None, "candidate_care_event_ids": []}
    base = {"org_id": DEFAULT_ORG_ID, "patient_id": profile["patient_id"], "event_type": "Entleistung"}
    exact = []
    if item.get("original_invoice_number"):
        exact = list(database.care_events.find({**base, "source_invoice_number": item["original_invoice_number"]}))
    candidates = exact or [event for event in database.care_events.find(base)
                           if str(event.get("care_account", "")) == "4064"
                           and _overlaps_care_period(event, item.get("service_period_start"), item.get("service_period_end"))]
    identifiers = [candidate["care_event_id"] for candidate in candidates]
    if len(candidates) == 1:
        return {"match_status": "matched", "match_method": "source_invoice+insurance" if exact else "insurance+period",
                "patient_id": profile["patient_id"], "matched_care_event_id": identifiers[0], "candidate_care_event_ids": identifiers}
    return {"match_status": "ambiguous" if candidates else "unmatched", "match_method": None,
            "patient_id": profile["patient_id"], "candidate_care_event_ids": identifiers}


@transactional
def register_batch(source_file_name: str, source_pdf_hash: str, parser_version: str) -> dict:
    """Register a source statement once; parser upgrades append observations."""
    database = get_database()
    identity = {"org_id": DEFAULT_ORG_ID, "source_pdf_hash": source_pdf_hash}
    now = datetime.now(timezone.utc)
    existing = database.rzh_reconciliation_batches.find_one(identity)
    if existing:
        database.rzh_reconciliation_batches.update_one(identity, {"$addToSet": {"parser_versions": parser_version},
            "$set": {"last_parsed_at": now}})
        return {**existing, "already_registered": True}
    batch = {**identity, "batch_id": generate_id("rrb"), "source_file_name": source_file_name,
        "parser_versions": [parser_version], "parse_status": "registered", "item_count": 0,
        "matched_count": 0, "manual_review_count": 0, "created_at": now, "last_parsed_at": now}
    database.rzh_reconciliation_batches.insert_one(batch)
    return {**batch, "already_registered": False}


@transactional
def persist_settlement_observations(batch: dict, observations: list[dict], warnings: list[str], parser_version: str) -> dict:
    """Upsert evidence and matches only; never apply a financial consequence."""
    database = get_database()
    matched = 0
    for observation in observations:
        match = match_item(observation, database)
        fingerprint = settlement_item_fingerprint(observation)
        now = datetime.now(timezone.utc)
        # created_at is set-on-insert only; updated_at and parser_versions are
        # written on every observation (insert or repeat) via $set/$addToSet,
        # so they must NOT also appear in $setOnInsert -- MongoDB rejects an
        # update that targets the same field path from two different
        # top-level operators (mongomock does not enforce this, which is why
        # this previously passed every test but fails against real MongoDB).
        document = {**observation, **match, "org_id": DEFAULT_ORG_ID, "item_fingerprint": fingerprint,
            "batch_id": batch["batch_id"], "reconciliation_item_id": generate_id("rri"),
            "apply_status": "not_applied", "created_at": now}
        database.rzh_reconciliation_items.update_one(
            {"org_id": DEFAULT_ORG_ID, "item_fingerprint": fingerprint},
            {"$setOnInsert": document, "$set": {"last_observed_batch_id": batch["batch_id"], "updated_at": now},
             "$addToSet": {"parser_versions": parser_version}}, upsert=True)
        if match["match_status"] == "matched":
            matched += 1
    item_count = database.rzh_reconciliation_items.count_documents({"org_id": DEFAULT_ORG_ID, "batch_id": batch["batch_id"]})
    parse_status = "incomplete" if warnings else "parsed"
    database.rzh_reconciliation_batches.update_one({"org_id": DEFAULT_ORG_ID, "batch_id": batch["batch_id"]},
        {"$set": {"parse_status": parse_status, "item_count": item_count, "matched_count": matched,
                  "manual_review_count": len(warnings), "warnings": list(dict.fromkeys(warnings)),
                  "last_parsed_at": datetime.now(timezone.utc)}, "$addToSet": {"parser_versions": parser_version}})
    return {"parsed_items": len(observations), "matched_items": matched, "manual_review_items": len(warnings),
            "financial_effects_applied": False}


@transactional
def manually_match_item(reconciliation_item_id: str, care_event_id: str) -> dict:
    """Record an explicit staff match without applying settlement consequences."""
    database = get_database()
    item = database.rzh_reconciliation_items.find_one({"org_id": DEFAULT_ORG_ID,
                                                         "reconciliation_item_id": reconciliation_item_id})
    if not item:
        raise HTTPException(404, "Reconciliation item not found")
    if item.get("apply_status") not in {"not_applied", "proposed", "reviewed"}:
        raise HTTPException(409, "Applied reconciliation items cannot be rematched")
    event = database.care_events.find_one({"org_id": DEFAULT_ORG_ID, "care_event_id": care_event_id,
                                           "event_type": "Entleistung", "care_account": "4064"})
    if not event:
        raise HTTPException(422, "Selected care event is not an Entlastungsleistung 4064 event")
    profile = database.patient_profiles.find_one({"org_id": DEFAULT_ORG_ID, "patient_id": event["patient_id"]})
    if not profile or profile.get("insurance_number") != item.get("insurance_number"):
        raise HTTPException(422, "Selected care event belongs to a different patient")
    database.rzh_reconciliation_items.update_one({"_id": item["_id"]}, {"$set": {
        "patient_id": event["patient_id"], "matched_care_event_id": care_event_id, "candidate_care_event_ids": [care_event_id],
        "match_status": "matched", "match_method": "manual", "updated_at": datetime.now(timezone.utc)}})
    return {"status": "matched", "reconciliation_item_id": reconciliation_item_id, "care_event_id": care_event_id,
            "financial_effects_applied": False}


@transactional
def retry_unresolved_matches() -> dict:
    """Reconsider unresolved settlement evidence after later care-event imports.

    Manual matches and reviewed decisions remain authoritative. This only updates
    match metadata and cannot create an invoice or change financial values.
    """
    database = get_database()
    rows = list(database.rzh_reconciliation_items.find({
        "org_id": DEFAULT_ORG_ID,
        "apply_status": {"$in": ["not_applied", "proposed"]},
        "match_status": {"$in": ["unmatched", "ambiguous"]},
    }))
    newly_matched = 0
    still_unresolved = 0
    for item in rows:
        match = match_item(item, database)
        before_status = item.get("match_status")
        updates = {
            "match_status": match["match_status"],
            "match_method": match.get("match_method"),
            "candidate_care_event_ids": match.get("candidate_care_event_ids", []),
            "updated_at": datetime.now(timezone.utc),
        }
        if match.get("patient_id") is not None:
            updates["patient_id"] = match["patient_id"]
        else:
            database.rzh_reconciliation_items.update_one({"_id": item["_id"]}, {"$unset": {"patient_id": ""}})
        if match.get("matched_care_event_id") is not None:
            updates["matched_care_event_id"] = match["matched_care_event_id"]
        else:
            database.rzh_reconciliation_items.update_one({"_id": item["_id"]}, {"$unset": {"matched_care_event_id": ""}})
        database.rzh_reconciliation_items.update_one({"_id": item["_id"]}, {"$set": updates})
        if before_status != "matched" and match["match_status"] == "matched":
            newly_matched += 1
        if match["match_status"] != "matched":
            still_unresolved += 1
    return {"status": "completed", "rescanned": len(rows), "newly_matched": newly_matched,
            "still_unresolved": still_unresolved, "financial_effects_applied": False}


def item_detail(reconciliation_item_id: str, database=None) -> dict:
    """Everything needed to show one reconciliation item expanded: the exact
    RZH-statement evidence for the reduction (Korrektur), the underlying
    claim it was matched to (Leistung), and that claim's own billing state --
    read-only, no financial effects.
    """
    database = database if database is not None else get_database()
    item = database.rzh_reconciliation_items.find_one({"org_id": DEFAULT_ORG_ID,
                                                         "reconciliation_item_id": reconciliation_item_id})
    if not item:
        raise HTTPException(404, "Reconciliation item not found")

    correction = {key: item.get(key) for key in (
        "reconciliation_item_id", "section_type", "reason_code", "reason_raw", "amount_cents",
        "rzh_transaction_number", "original_invoice_number", "service_period_start", "service_period_end",
        "patient_name_raw", "insurance_number", "care_account", "match_status", "match_method",
        "apply_status", "candidate_care_event_ids", "created_at", "last_observed_batch_id",
    )}
    batch = database.rzh_reconciliation_batches.find_one({"org_id": DEFAULT_ORG_ID, "batch_id": item.get("batch_id")})
    correction["source_file_name"] = batch.get("source_file_name") if batch else None

    claim = None
    if item.get("matched_care_event_id"):
        event = database.care_events.find_one({"org_id": DEFAULT_ORG_ID, "care_event_id": item["matched_care_event_id"]})
        if event:
            bill = database.billing_details.find_one({"org_id": DEFAULT_ORG_ID, "care_event_id": event["care_event_id"]})
            patient = database.patient_profiles.find_one({"org_id": DEFAULT_ORG_ID, "patient_id": event["patient_id"]})
            claim = {
                "care_event_id": event["care_event_id"],
                "patient_id": event["patient_id"],
                "patient_name": patient.get("patient_name") if patient else None,
                "insurance_number": patient.get("insurance_number") if patient else None,
                "invoicing_month": event.get("invoicing_month"),
                "period_start_date": event.get("period_start_date"),
                "period_end_date": event.get("period_end_date"),
                "care_account": event.get("care_account"),
                "sum_total": event.get("sum_total"),
                "services": event.get("services", []),
                "billing_detail_id": bill.get("billing_detail_id") if bill else None,
                "sum_covered": bill.get("sum_covered") if bill else None,
                "amount_owed": bill.get("amount_owed") if bill else None,
                "billing_status": bill.get("billing_status") if bill else None,
                "reconciliation_status": bill.get("reconciliation_status") if bill else None,
                "confirmed_sum_covered": bill.get("confirmed_sum_covered") if bill else None,
                "confirmed_amount_owed": bill.get("confirmed_amount_owed") if bill else None,
            }
    return {"status": "ok", "correction": correction, "claim": claim}


def eligible_invoice_queue(database=None) -> list[dict]:
    """Return confirmed, unissued Entlastung claims across service months."""
    database = database if database is not None else get_database()
    bills = database.billing_details.find({"org_id": DEFAULT_ORG_ID, "billing_status": "invoice_needed",
                                           "reconciliation_status": {"$in": list(CONFIRMED_ENTLASTUNG_STATUSES)},
                                           "pdf_path": {"$in": [None, ""]}, "amount_owed": {"$gt": 0}})
    queue = []
    for bill in bills:
        event = database.care_events.find_one({"org_id": DEFAULT_ORG_ID, "care_event_id": bill["care_event_id"],
                                               "event_type": "Entleistung"})
        if not event:
            continue
        patient = database.patient_profiles.find_one({"org_id": DEFAULT_ORG_ID, "patient_id": event["patient_id"]})
        queue.append({"billing_detail_id": bill["billing_detail_id"], "care_event_id": bill["care_event_id"],
                      "patient_id": event["patient_id"], "patient_name": patient.get("patient_name") if patient else None,
                      "service_month": bill["invoicing_month"],
                      "period_start_date": event.get("period_start_date"), "period_end_date": event.get("period_end_date"),
                      "confirmed_amount_owed": bill["amount_owed"], "reconciliation_status": bill["reconciliation_status"]})
    return sorted(queue, key=lambda row: (row["period_start_date"] or "", row["billing_detail_id"]))


def invoice_selection_queue(database=None) -> list[dict]:
    """Retired: private Entlastungsleistung invoices require RZH confirmation."""
    return []


@transactional
def approve_invoice_selection(billing_detail_ids: list[str], actor: str) -> dict:
    raise HTTPException(410, "Estimated Entlastungsleistung invoices are retired; wait for RZH confirmation")


def issue_confirmed_invoice(billing_detail_id: str) -> dict:
    """Render one staff-selected, RZH-confirmed invoice from the cross-month queue."""
    database = get_database()
    bill = database.billing_details.find_one({"org_id": DEFAULT_ORG_ID, "billing_detail_id": billing_detail_id})
    if not bill:
        raise HTTPException(404, "Billing detail not found")
    if bill.get("pdf_path"):
        return {"status": "already_generated", "billing_detail_id": billing_detail_id,
                "invoice_number": bill.get("invoice_number")}
    event = database.care_events.find_one({"org_id": DEFAULT_ORG_ID, "care_event_id": bill.get("care_event_id")})
    if not event:
        raise HTTPException(404, "Care event not found")
    if (
        event.get("event_type") != "Entleistung"
        or bill.get("reconciliation_status") not in CONFIRMED_ENTLASTUNG_STATUSES
        or bill.get("amount_owed", 0) <= 0
    ):
        raise HTTPException(409, "Billing detail is not an RZH-confirmed Entlastungsleistung invoice")
    assert_invoice_eligible(bill, event)
    from app.invoice_generator import generate_billing_pdf

    generate_billing_pdf(billing_detail_id)
    refreshed = database.billing_details.find_one({"org_id": DEFAULT_ORG_ID, "billing_detail_id": billing_detail_id})
    return {"status": "generated", "billing_detail_id": billing_detail_id,
            "invoice_number": refreshed.get("invoice_number")}


@transactional
def confirm_private_amount(reconciliation_item_id: str, actor: str) -> dict:
    """Apply a staff-reviewed claim-level settlement assessment.

    This changes only the billing detail used by the invoice workflow. It does
    not alter the annual ledger or create a replay/checkpoint.
    """
    database = get_database()
    item = database.rzh_reconciliation_items.find_one({"org_id": DEFAULT_ORG_ID,
                                                         "reconciliation_item_id": reconciliation_item_id})
    if not item:
        raise HTTPException(404, "Reconciliation item not found")
    if item.get("match_status") != "matched" or not item.get("matched_care_event_id"):
        raise HTTPException(409, "A matched care event is required before confirming a private amount")
    if item.get("reason_code") not in MANUALLY_CONFIRMABLE_REASONS or item.get("amount_cents", 0) >= 0:
        raise HTTPException(422, "This settlement reason cannot confirm a private Entlastungsleistung amount")
    if item.get("apply_status") not in {"not_applied", "proposed", "reviewed"}:
        raise HTTPException(409, "This settlement item has already been finalized")
    event = database.care_events.find_one({"org_id": DEFAULT_ORG_ID, "care_event_id": item["matched_care_event_id"],
                                           "event_type": "Entleistung", "care_account": "4064"})
    if not event:
        raise HTTPException(422, "Matched care event no longer exists")
    bill = database.billing_details.find_one({"org_id": DEFAULT_ORG_ID, "care_event_id": event["care_event_id"]})
    if not bill:
        # Historic claims may predate billing readiness. The matched event and
        # RZH item are already the audited authority, so create the durable
        # zero-value marker that confirmation updates below.
        now = datetime.now(timezone.utc)
        bill = {
            "org_id": DEFAULT_ORG_ID,
            "billing_detail_id": generate_id("bill"),
            "care_event_id": event["care_event_id"],
            "invoicing_month": event.get("invoicing_month"),
            "sum_total": round(float(event.get("sum_total", 0)), 2),
            "sum_covered": round(float(event.get("sum_total", 0)), 2),
            "amount_owed": 0.0,
            "investitionskosten": 0.0,
            "invoice_number": None,
            "billing_status": "covered_insurance",
            "reconciliation_status": "assumed_covered_until_rzh",
            "coverage_source": "assumed_full_until_rzh",
            "created_at": now,
        }
        database.billing_details.insert_one(bill)
    if item.get("apply_status") == "reviewed" and bill.get("reconciliation_item_id") == reconciliation_item_id:
        return {"status": "confirmed", "already_confirmed": True, "billing_detail_id": bill["billing_detail_id"],
                "confirmed_amount_owed": bill.get("confirmed_amount_owed", bill.get("amount_owed")),
                "ledger_effects_applied": False}
    if billing_detail_already_issued(bill):
        raise HTTPException(409, "Issued invoice requires an explicit accounting correction")
    if bill.get("reconciliation_item_id") not in {None, reconciliation_item_id}:
        raise HTTPException(409, "A different settlement item already controls this billing detail")
    confirmed_owed = round(abs(item["amount_cents"]) / 100, 2)
    total = round(float(bill.get("sum_total", event.get("sum_total", 0))), 2)
    if confirmed_owed <= 0 or confirmed_owed > total:
        raise HTTPException(422, "Settlement amount is inconsistent with the service total")
    confirmed_covered = round(total - confirmed_owed, 2)
    reconciliation_status = "confirmed_limit_partial" if confirmed_covered else "confirmed_limit_full"
    now = datetime.now(timezone.utc)
    updates = {
        "sum_total": total,
        "sum_covered": confirmed_covered,
        "amount_owed": confirmed_owed,
        "billing_status": "invoice_needed",
        "reconciliation_status": reconciliation_status,
        "reconciliation_item_id": reconciliation_item_id,
        "confirmed_sum_covered": confirmed_covered,
        "confirmed_amount_owed": confirmed_owed,
        "coverage_source": "rzh_confirmed",
        "reconciled_at": now,
        "updated_at": now,
    }
    if "estimated_sum_covered" not in bill:
        updates["estimated_sum_covered"] = bill.get("sum_covered", 0)
        updates["estimated_amount_owed"] = bill.get("amount_owed", 0)
        updates["estimated_at"] = bill.get("created_at", now)
    database.billing_details.update_one({"_id": bill["_id"]}, {"$set": updates})
    database.rzh_reconciliation_items.update_one({"_id": item["_id"]}, {"$set": {
        "apply_status": "reviewed", "entitlement_action": "confirm_private_amount", "confirmed_amount_owed": confirmed_owed,
        "confirmed_sum_covered": confirmed_covered, "reviewed_at": now, "reviewed_by": actor, "updated_at": now}})
    database.care_event_history.insert_one({"org_id": DEFAULT_ORG_ID, "care_event_id": event["care_event_id"],
        "action": "rzh_private_amount_confirmed", "created_at": now, "actor": actor,
        "before": {key: bill.get(key) for key in ("sum_covered", "amount_owed", "billing_status", "reconciliation_status")},
        "after": {"sum_covered": confirmed_covered, "amount_owed": confirmed_owed,
                  "billing_status": "invoice_needed", "reconciliation_status": reconciliation_status},
        "reconciliation_item_id": reconciliation_item_id})
    return {"status": "confirmed", "billing_detail_id": bill["billing_detail_id"],
            "confirmed_amount_owed": confirmed_owed, "ledger_effects_applied": False}


@transactional
def dismiss_item(reconciliation_item_id: str, actor: str, reason: str) -> dict:
    """Close out a reconciliation item without a private-amount confirmation.

    For items staff decide need no further action through this workflow --
    most commonly because the underlying claim already has an issued invoice
    and needs a manual accounting correction instead (see
    billing_detail_already_issued), or because the reduction genuinely
    doesn't warrant a patient charge. Only sets apply_status/reason/actor on
    the reconciliation item itself: never touches a billing_detail, invoice,
    or entlastung_year_balance, and the item stays fully visible with its
    evidence intact -- this stops it counting as an open review case without
    hiding or deleting anything.
    """
    if not reason or not reason.strip():
        raise HTTPException(422, "A reason is required to close a reconciliation item without action")
    database = get_database()
    item = database.rzh_reconciliation_items.find_one({"org_id": DEFAULT_ORG_ID,
                                                         "reconciliation_item_id": reconciliation_item_id})
    if not item:
        raise HTTPException(404, "Reconciliation item not found")
    if item.get("apply_status") not in {"not_applied", "proposed", "reviewed"}:
        raise HTTPException(409, "This settlement item has already been finalized or dismissed")
    now = datetime.now(timezone.utc)
    database.rzh_reconciliation_items.update_one({"_id": item["_id"]}, {"$set": {
        "apply_status": "dismissed", "dismissed_reason": reason.strip(), "dismissed_at": now,
        "dismissed_by": actor, "updated_at": now}})
    if item.get("matched_care_event_id"):
        database.care_event_history.insert_one({"org_id": DEFAULT_ORG_ID, "care_event_id": item["matched_care_event_id"],
            "action": "rzh_reconciliation_item_dismissed", "created_at": now, "actor": actor,
            "reason": reason.strip(), "reconciliation_item_id": reconciliation_item_id})
    return {"status": "dismissed", "reconciliation_item_id": reconciliation_item_id, "financial_effects_applied": False}
