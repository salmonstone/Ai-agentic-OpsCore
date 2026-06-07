"""
ChromaDB vector store with Voyage AI semantic embeddings.

When VOYAGE_API_KEY is set in .env, uses voyage-3-lite (real semantic search).
Falls back to a pure-Python hash embedding when the key is absent — same
interface, no crashes, just weaker similarity matching.

Voyage AI free tier: 200M tokens/month — enough for years of agent memory.
Get a key at: https://www.voyageai.com  (takes 30 seconds, no credit card)
"""
from __future__ import annotations

import hashlib
import math
import os

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import chromadb
import chromadb.api.types as chroma_types

from agent.observability.logging import get_logger

log = get_logger(__name__)

_COLLECTION_NAME = "agent_memories"
_CHROMA_PATH     = "chroma_db"
_DIM             = 512   # voyage-3-lite output dimension


# ---------------------------------------------------------------------------
# Embedding functions
# ---------------------------------------------------------------------------

class _HashEmbeddingFunction(chroma_types.EmbeddingFunction):
    """Fallback: word-hash bag-of-words, pure Python, zero dependencies."""

    def __call__(self, input: chroma_types.Documents) -> chroma_types.Embeddings:
        results = []
        for text in input:
            vec = [0.0] * _DIM
            for word in text.lower().split():
                idx = int.from_bytes(hashlib.md5(word.encode()).digest()[:4], "little") % _DIM
                vec[idx] += 1.0
            norm = math.sqrt(sum(x * x for x in vec))
            if norm > 0:
                vec = [x / norm for x in vec]
            results.append(vec)
        return results


class _VoyageEmbeddingFunction(chroma_types.EmbeddingFunction):
    """
    Real semantic embeddings via Voyage AI voyage-3-lite.
    Batches up to 128 texts per API call.
    """

    def __init__(self, api_key: str) -> None:
        import voyageai
        self._client = voyageai.Client(api_key=api_key)

    def __call__(self, input: chroma_types.Documents) -> chroma_types.Embeddings:
        result = self._client.embed(
            list(input),
            model="voyage-3-lite",
            input_type="document",
        )
        return result.embeddings


def _make_embedding_fn() -> chroma_types.EmbeddingFunction:
    from agent.config import settings
    key = settings.voyage_api_key or os.environ.get("VOYAGE_API_KEY", "")
    if key:
        try:
            fn = _VoyageEmbeddingFunction(api_key=key)
            log.info("memory.embeddings.mode", mode="voyage-3-lite")
            return fn
        except Exception as exc:
            log.warning("memory.embeddings.voyage_failed",
                        error=str(exc)[:120],
                        hint="Falling back to hash embeddings")
    log.info("memory.embeddings.mode", mode="hash-fallback",
             hint="Set VOYAGE_API_KEY in .env for semantic search")
    return _HashEmbeddingFunction()


# ---------------------------------------------------------------------------
# ChromaDB client / collection (lazy singletons)
# ---------------------------------------------------------------------------

_client:             chromadb.PersistentClient | None = None
_collection:         chromadb.Collection | None       = None
_chroma_unavailable: bool                             = False


def _get_collection() -> chromadb.Collection | None:
    global _client, _collection, _chroma_unavailable
    if _chroma_unavailable:
        return None
    if _collection is None:
        try:
            _client     = chromadb.PersistentClient(path=_CHROMA_PATH)
            _collection = _client.get_or_create_collection(
                name=_COLLECTION_NAME,
                embedding_function=_make_embedding_fn(),
                metadata={"hnsw:space": "cosine"},
            )
            log.info("memory.embeddings.collection_ready",
                     name=_COLLECTION_NAME, path=_CHROMA_PATH)
        except Exception as exc:
            _chroma_unavailable = True
            log.warning("memory.embeddings.unavailable", error=str(exc)[:120])
            return None
    return _collection


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def add_embedding(memory_id: str, content: str, metadata: dict | None = None) -> None:
    col = _get_collection()
    if col is None:
        return
    col.upsert(ids=[memory_id], documents=[content], metadatas=[metadata or {}])
    log.debug("memory.embeddings.added", id=memory_id)


def search_similar(query: str, limit: int = 5) -> list[dict]:
    """
    Return the *limit* most semantically similar stored memories.
    Each result: { id, content, metadata, distance }
    Distance is cosine — 0 = identical, 1 = orthogonal.
    """
    col = _get_collection()
    if col is None:
        return []
    if col.count() == 0:
        return []

    n       = min(limit, col.count())
    results = col.query(query_texts=[query], n_results=n)

    return [
        {
            "id":       mem_id,
            "content":  results["documents"][0][i],
            "metadata": results["metadatas"][0][i],
            "distance": round(results["distances"][0][i], 4),
        }
        for i, mem_id in enumerate(results["ids"][0])
    ]


def delete_embedding(memory_id: str) -> None:
    col = _get_collection()
    if col is None:
        return
    try:
        col.delete(ids=[memory_id])
        log.info("memory.embeddings.deleted", id=memory_id)
    except Exception:
        pass
