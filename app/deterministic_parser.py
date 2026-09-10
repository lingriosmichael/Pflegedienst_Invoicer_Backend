"""
Deterministic (non-LLM) structured extraction for RZH Honorarabrechnung PDFs.

STANDALONE / EXPERIMENTAL: this module is not imported by the live import
pipeline (app/pdf_parser.py, app/routers/imports.py). It exists so the
regex-based extraction can be validated against the existing OpenAI-based
extraction before anyone decides to cut the import path over to it.

It reuses app.pdf_parser.extract_text_from_pdf / split_into_chunks unchanged
(same chunk boundaries as the live pipeline) and produces the exact same
record shape as app.openai_client.extract_structured_data_with_openai:

    {
      "patient": {"name", "birthdate", "insurance_number", "care_level", "pflege_konto"},
      "services": [{"quantity", "code", "description", "unit_price", "total_price"}, ...],
      "invoice": {"pflegezeitraum_beginn", "pflegezeitraum_ende", "summe_covered", "summe_total"},
    }

so that, if it proves reliable, it can be dropped into
app.pdf_parser.process_import_records in place of the OpenAI call with no
changes needed downstream (app.import_records.persist_record and friends).

Failure policy: fails CLOSED. Any chunk that does not match the expected RZH
table grammar raises DeterministicParseError rather than guessing -- the
caller is expected to treat that the same way process_import_records treats
an extraction failure today (record it, do not persist it).
"""

import re


class DeterministicParseError(ValueError):
    """A chunk did not match the expected RZH table grammar. Never caught and
    papered over silently -- callers should surface this like any other
    extraction failure."""


# ---------------------------------------------------------------------------
# Boilerplate stripping
#
# When a patient's service block straddles a PDF page boundary, PyMuPDF's
# page-by-page get_text() concatenation interleaves the RZH letterhead
# footer/header into the middle of the chunk. These lines are constant across
# every page of a given statement, so they can be filtered out safely.
# ---------------------------------------------------------------------------

_FOOTER_LITERAL_LINES = {
    "Geschäftsführer:",
    "Registergericht Duisburg HRB 11634",
    "USt-IdNr. DE216882508",
    "E-Mail info@rzh.de",
    "Internet www.rzh.de",
    "Standort Oldenburg",
    "Birkenweg 3",
    "26127 Oldenburg",
    "Telefon 0281/9885-222",
    "Telefax 0441/9220-910",
    "Standort Hannover",
    "Wohlenbergstraße 4D",
    "30179 Hannover",
    "Telefon 0281/9885-270",
    "Telefax 0511/67400-77",
    "Postanschrift Wesel",
    "Philipp-Reis-Straße 7-9",
    "46485 Wesel",
    "Telefon 0281/9885-0",
    "Telefax 0281/9885-114",
    "Sitz der Gesellschaft",
    "RZH Rechenzentrum",
    "für Heilberufe GmbH",
    "Am Schornacker 32",
    "Detaillierte Aufstellung",
    "Abgerechnete Belege",
    "Kostenträger",
    "RZH Rechnungsnummer",
    "Betrag €",
}

_PAGE_MARKER = re.compile(r"^Seite\s+\d+$")
_DOC_HEADER = re.compile(r"^Kundennummer:.*Belegdatum:.*$")

# The repeated 5-line service-table column header. Only stripped on its
# second-and-later appearance within a chunk (a genuine first appearance must
# be kept as a boundary marker for locating the service rows).
_TABLE_HEADER = ["Anz Position", "E-Preis €", "Summe €", "MwSt%", "Zuzahlung €"]


def strip_boilerplate(lines):
    """Remove RZH page-footer/header lines from a list of stripped chunk lines."""
    out = []
    skip_signer_lines = 0
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        if skip_signer_lines > 0:
            skip_signer_lines -= 1
            i += 1
            continue

        if _PAGE_MARKER.match(line) or _DOC_HEADER.match(line) or line in _FOOTER_LITERAL_LINES:
            if line == "Geschäftsführer:":
                # Two signer-name lines always follow; names may change over
                # time so they are skipped positionally rather than by value.
                skip_signer_lines = 2
            i += 1
            continue

        if lines[i:i + len(_TABLE_HEADER)] == _TABLE_HEADER and _TABLE_HEADER[0] in out:
            i += len(_TABLE_HEADER)
            continue

        out.append(line)
        i += 1
    return out


# ---------------------------------------------------------------------------
# Field grammar
# ---------------------------------------------------------------------------

_PATIENT_RE = re.compile(
    r"^Patient:\s*(?P<name>.+?)\s*-\s*"
    r"(?:(?P<birthdate>\d{2}\.\d{2}\.(?:\d{2}|\d{4}))\s*-\s*)?"
    r"(?P<insurance>\S+)\s*-\s*Status:\s*(?P<status>\d+)"
    r"(?:\s*-\s*Pflegegrad:\s*(?P<care_level>\S+))?\s*$"
)

_VERORDNUNGSDATUM_RE = re.compile(
    r"^Verordnungsdatum:\s*(?P<verordnungsdatum>\d{2}\.\d{2}\.\d{2}),\s*"
    r"Pflegezeitraum:\s*(?P<beginn>\d{2}\.\d{2}\.\d{2})(?:\s*-\s*(?P<ende>\d{2}\.\d{2}\.\d{2}))?,\s*"
    r"Pflegekonto:\s*(?P<pflegekonto>\d{4})\s*$"
)

_SERVICE_START_RE = re.compile(r"^(?P<quantity>\d+(?:,\d+)?)\s+(?P<code>\S+)\s+(?P<description>.+)$")
_AMOUNT_RE = re.compile(r"^-?[\d.]+,\d{2}$")


def parse_chunk(raw_chunk_text):
    """Parse one 'Verordnung: ...' chunk (as produced by
    app.pdf_parser.split_into_chunks) into the INVOICE_SCHEMA record shape.

    Raises DeterministicParseError on anything unexpected -- never guesses.
    """
    lines = [line.strip() for line in raw_chunk_text.splitlines() if line.strip()]
    lines = strip_boilerplate(lines)

    if not lines or not lines[0].startswith("Verordnung:"):
        raise DeterministicParseError("chunk does not start with 'Verordnung:'")

    patient_line = next((line for line in lines if line.startswith("Patient:")), None)
    if patient_line is None:
        raise DeterministicParseError("missing 'Patient:' line")
    patient_match = _PATIENT_RE.match(patient_line)
    if not patient_match:
        raise DeterministicParseError(f"unrecognized Patient line: {patient_line!r}")

    verord_line = next((line for line in lines if line.startswith("Verordnungsdatum:")), None)
    if verord_line is None:
        raise DeterministicParseError("missing 'Verordnungsdatum:' line")
    verord_match = _VERORDNUNGSDATUM_RE.match(verord_line)
    if not verord_match:
        raise DeterministicParseError(f"unrecognized Verordnungsdatum line: {verord_line!r}")

    beginn = verord_match.group("beginn")
    # RZH omits the end date entirely when the Pflegezeitraum is a single day.
    ende = verord_match.group("ende") or beginn

    zuzahlung_positions = [index for index, line in enumerate(lines) if line == "Zuzahlung €"]
    if not zuzahlung_positions:
        raise DeterministicParseError("missing 'Zuzahlung €' column header")
    start = zuzahlung_positions[-1] + 1

    try:
        summe_idx = lines.index("Summe €", start)
    except ValueError:
        raise DeterministicParseError("missing terminal 'Summe €' line") from None

    service_lines = lines[start:summe_idx]
    services = []
    i = 0
    while i < len(service_lines):
        match = _SERVICE_START_RE.match(service_lines[i])
        if not match:
            raise DeterministicParseError(f"unrecognized service line: {service_lines[i]!r}")
        if i + 2 >= len(service_lines):
            raise DeterministicParseError("truncated service block (missing price lines)")
        unit_price, total_price = service_lines[i + 1], service_lines[i + 2]
        if not _AMOUNT_RE.match(unit_price) or not _AMOUNT_RE.match(total_price):
            raise DeterministicParseError(
                f"unrecognized amounts after service line: {unit_price!r} {total_price!r}"
            )
        services.append({
            "quantity": match.group("quantity"),
            "code": match.group("code"),
            "description": match.group("description").strip(),
            "unit_price": unit_price,
            "total_price": total_price,
        })
        i += 3

    if not services:
        raise DeterministicParseError("no service lines found")

    if summe_idx + 2 >= len(lines):
        raise DeterministicParseError("missing totals after 'Summe €'")
    summe_total, summe_covered = lines[summe_idx + 1], lines[summe_idx + 2]
    if not _AMOUNT_RE.match(summe_total) or not _AMOUNT_RE.match(summe_covered):
        raise DeterministicParseError(f"unrecognized invoice totals: {summe_total!r} {summe_covered!r}")

    return {
        "patient": {
            "name": patient_match.group("name").strip(),
            "birthdate": patient_match.group("birthdate") or "",
            "insurance_number": patient_match.group("insurance"),
            "care_level": patient_match.group("care_level") or "",
            "pflege_konto": verord_match.group("pflegekonto"),
        },
        "services": services,
        "invoice": {
            "pflegezeitraum_beginn": beginn,
            "pflegezeitraum_ende": ende,
            "summe_covered": summe_covered,
            "summe_total": summe_total,
        },
    }


def parse_pdf(path):
    """Run the deterministic parser over an entire PDF using the exact same
    chunk boundaries the live LLM pipeline would use, so results are directly
    comparable to (and swappable with) app.pdf_parser.process_import_records.

    Returns a list of {"index", "status", "record", "error"} dicts, one per
    chunk, in chunk order. Never touches the database or the OpenAI client.
    """
    from app import pdf_parser

    text = pdf_parser.extract_text_from_pdf(path)
    chunks = pdf_parser.split_into_chunks(text)
    results = []
    for index, chunk_text in enumerate(chunks):
        try:
            record = parse_chunk(chunk_text)
            results.append({"index": index, "status": "ok", "record": record, "error": None})
        except DeterministicParseError as error:
            results.append({"index": index, "status": "failed", "record": None, "error": str(error)})
    return results
