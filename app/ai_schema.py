import sqlite3
from typing import Dict, Any, List
from fastapi import APIRouter
from app.database import DB_PATH

router = APIRouter(prefix="/ai", tags=["ai"])

def _connect():
    con = sqlite3.connect(DB_PATH, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    con.execute("PRAGMA cache_size = -64000")
    con.execute("PRAGMA foreign_keys = ON")
    return con

def _list_tables(cur) -> List[str]:
    rows = cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return [r["name"] for r in rows]

def _table_info(cur, table: str):
    cols = cur.execute(f"PRAGMA table_info({table})").fetchall()
    columns = []
    pk = None
    for c in cols:
        columns.append({
            "name": c["name"],
            "type": c["type"],
            "notnull": bool(c["notnull"]),
            "default": c["dflt_value"],
            "pk": bool(c["pk"]),
        })
        if c["pk"] == 1:
            pk = c["name"]

    fks_raw = cur.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    fks = []
    for fk in fks_raw:
        fks.append({
            "column": fk["from"],
            "ref_table": fk["table"],
            "ref_column": fk["to"],
        })
    return pk, columns, fks

def _sample_rows(cur, table: str, n: int = 3):
    try:
        rows = cur.execute(f"SELECT * FROM {table} LIMIT {n}").fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []

# Semantic mappings — helps GPT map natural German words to column names
SEMANTIC_HINTS = {
    "patients": {
        "name": ["Patient", "Name"],
        "birthdate": ["Geburtsdatum", "Geburtsdatum des Patienten"],
        "insurance_number": ["Versicherungsnummer", "Kassennummer"],
        "care_level": ["Pflegegrad", "Pflegestufe"],
        "address": ["Adresse", "Wohnort"],
        "debtor_number": ["Debitorennummer", "Debitor"],
    },
    "invoices": {
        "patient_id": ["Patient-ID", "Patientenreferenz"],
        "care_range_begin": ["Pflegezeitraum Beginn", "Startdatum"],
        "care_range_end": ["Pflegezeitraum Ende", "Enddatum"],
        "sum_covered": ["Summe übernommen", "Kassenanteil", "Abgedeckt"],
        "sum_total": ["Gesamtsumme", "Gesamtbetrag"],
        "amount_owed": ["Betrag offen", "Eigenanteil", "Privatrechnung"],
        "care_account": ["Pflegekonto", "Leistungstyp", "SGBXI", "Entlastungsleistung"],
        "invoicing_month": ["Abrechnungsmonat", "Monat"],
        "invoice_number": ["Rechnungsnummer"],
        "private_rechnung": ["Markiert für Privatrechnung", "privat"],
    },
    "services": {
        "invoice_id": ["Rechnungs-ID", "Rechnung"],
        "quantity": ["Anzahl", "Menge"],
        "code": ["Leistungscode", "Kürzel"],
        "description": ["Beschreibung", "Leistungsbeschreibung"],
        "unit_price": ["Einzelpreis"],
        "total_price": ["Gesamtpreis", "Summe Position"],
    },
}

@router.get("/schema")
def get_ai_schema() -> Dict[str, Any]:
    """
    Returns the database schema for AI reasoning.
    Includes:
    - Tables
    - Primary and foreign keys
    - Relationships
    - Example rows
    - Semantic hints for column names
    """
    con = _connect()
    cur = con.cursor()
    tables = _list_tables(cur)

    tables_map: Dict[str, Any] = {}
    relationships: List[Dict[str, str]] = []

    for t in tables:
        pk, columns, fks = _table_info(cur, t)
        samples = _sample_rows(cur, t, 3)
        tables_map[t] = {
            "primary_key": pk,
            "columns": columns,
            "foreign_keys": fks,
            "row_examples": samples,
        }
        for fk in fks:
            relationships.append({
                "from_table": t,
                "from_column": fk["column"],
                "to_table": fk["ref_table"],
                "to_column": fk["ref_column"],
            })

    con.close()

    return {
        "db": "sqlite",
        "tables": tables_map,
        "relationships": relationships,
        "semantic_hints": SEMANTIC_HINTS,
        "description": "This database contains patients, invoices, and services for a German Pflegedienst. Relationships: invoices → patients (N:1), services → invoices (N:1).",
    }
