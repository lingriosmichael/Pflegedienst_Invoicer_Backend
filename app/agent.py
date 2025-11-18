import logging
from typing import Any, Dict, List, Optional

from app.rag_store import RagStore
from app.db.connection import get_db
from app.openai_client import client as openai_client, safe_parse_json

logger = logging.getLogger(__name__)

PROMPT_SYSTEM = """
You are a billing assistant for a German Pflegedienst. Use the provided patient chunks
as evidence to answer the user's question. Always include the chunk_id(s) you relied on.
Focus on accuracy, cite evidence_chunk_ids in order, and ensure monetary values use the German
format (decimal with a comma) if possible.
"""

PROMPT_TEMPLATE = """
Available context ({chunk_count} chunks):

{chunk_context}

Question:
{question}

Respond strictly as JSON:
{{
  "answer": "Concise answer to the question.",
  "evidence_chunk_ids": ["chunk_id1", "chunk_id2"],
  "needs_invoice": true | false,
  "estimated_amount": 123.45,
  "notes": "Optional clarification (if needed)."
}}
"""


def _fetch_chunk_metadata(chunk_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not chunk_ids:
        return {}

    placeholders = ",".join("?" for _ in chunk_ids)
    query = f"""
    SELECT id, patient_name, source_pdf, text_preview, created_at
    FROM chunks
    WHERE id IN ({placeholders})
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(query, tuple(chunk_ids))
        rows = cursor.fetchall()
    return {row[0]: {"patient_name": row[1], "source_pdf": row[2], "text_preview": row[3], "created_at": row[4]} for row in rows}


def _format_chunk_context(results: List[Dict[str, Any]], metadata: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    contexts = []
    for idx, result in enumerate(results, start=1):
        chunk_id = result.get("chunk_id")
        if not chunk_id:
            continue

        text = result.get("text") or metadata.get(chunk_id, {}).get("text_preview", "")
        block = f"=== CHUNK {idx} (chunk_id={chunk_id}; source={metadata.get(chunk_id, {}).get('source_pdf', 'unknown')}; patient={metadata.get(chunk_id, {}).get('patient_name', 'unknown')}) ===\n{text.strip()}"
        contexts.append({
            "chunk_id": chunk_id,
            "text": text,
            "source": metadata.get(chunk_id, {}).get("source_pdf"),
            "patient_name": metadata.get(chunk_id, {}).get("patient_name"),
            "context_block": block
        })
    return contexts


def run_agent(
    question: str, *,
    patient_name: Optional[str] = None,
    k: int = 4,
    model: str = "gpt-5-mini-2025-08-07"
) -> Dict[str, Any]:
    if openai_client is None:
        raise RuntimeError("OpenAI client not configured. Set OPENAI_API_KEY before using the agent.")

    rag = RagStore()
    where = {"patient_name": patient_name} if patient_name else None
    results = rag.query(question, k=k, where=where)
    chunk_ids = [r.get("chunk_id") for r in results if r.get("chunk_id")]
    metadata = _fetch_chunk_metadata(chunk_ids)
    contexts = _format_chunk_context(results, metadata)

    chunk_context_text = "\n\n".join(ctx["context_block"] for ctx in contexts)
    prompt = PROMPT_TEMPLATE.format(
        chunk_count=len(contexts),
        chunk_context=chunk_context_text or "No chunks available.",
        question=question
    )

    response = openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": PROMPT_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"}
    )

    llm_text = response.choices[0].message.content
    parsed = safe_parse_json(llm_text)
    if parsed is None:
        parsed = {"raw_response": llm_text}

    return {
        "question": question,
        "patient_name": patient_name,
        "chunk_count": len(contexts),
        "chunks": contexts,
        "agent_response": parsed,
        "raw_llm_text": llm_text,
    }
