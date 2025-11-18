import os
import logging
from typing import List, Dict, Any, Optional
import chromadb
from chromadb.config import Settings
from app.core.config import settings
from app.openai_client import client as openai_client

logger = logging.getLogger(__name__)

# Default chroma persist directory
CHROMA_DIR = os.getenv("CHROMA_PERSIST_DIR", "data/chroma")
COLLECTION_NAME = "pflegedienst_chunks"

# Choose embedding model name
EMBED_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

class RagStore:
    def __init__(self, persist_dir: Optional[str] = None):
        persist = persist_dir or CHROMA_DIR
        os.makedirs(persist, exist_ok=True)
        # Use duckdb+parquet persistent implementation via chromadb settings
        try:
            self.client = chromadb.Client(Settings(chroma_db_impl="duckdb+parquet", persist_directory=persist))
            self.col = self.client.get_or_create_collection(name=COLLECTION_NAME)
        except Exception as e:
            logger.error(f"Failed to initialize Chroma client: {e}")
            raise

    def embed_text(self, text: str) -> List[float]:
        """Compute embedding for a text using OpenAI embeddings via existing client."""
        if openai_client is None:
            raise RuntimeError("OpenAI client not configured. Set OPENAI_API_KEY in env.")

        try:
            resp = openai_client.embeddings.create(model=EMBED_MODEL, input=text)
            emb = resp.data[0].embedding
            return emb
        except Exception as e:
            logger.error(f"OpenAI embedding error: {e}")
            raise

    def add_chunk(self, chunk_id: str, text: str, metadata: Dict[str, Any]):
        """Add a single chunk into Chroma with precomputed embedding.
        This stores the vector and metadata; the canonical chunk row remains in SQLite.
        """
        emb = self.embed_text(text)
        try:
            self.col.add(ids=[chunk_id], embeddings=[emb], metadatas=[metadata], documents=[text])
        except Exception as e:
            logger.error(f"Failed to add chunk {chunk_id} to Chroma: {e}")
            raise

    def query(self, query_text: str, k: int = 5, where: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Query the Chroma collection and return list of results with ids, scores and metadata."""
        emb = self.embed_text(query_text)
        try:
            res = self.col.query(query_embeddings=[emb], n_results=k, where=where or {})
            # res contains 'ids', 'distances', 'metadatas', 'documents'
            results = []
            ids = res.get("ids", [[]])[0]
            distances = res.get("distances", [[]])[0]
            metadatas = res.get("metadatas", [[]])[0]
            documents = res.get("documents", [[]])[0]
            for i, cid in enumerate(ids):
                results.append({
                    "chunk_id": cid,
                    "score": float(distances[i]) if i < len(distances) else None,
                    "metadata": metadatas[i] if i < len(metadatas) else {},
                    "text": documents[i] if i < len(documents) else None,
                })
            return results
        except Exception as e:
            logger.error(f"Chroma query failed: {e}")
            return []