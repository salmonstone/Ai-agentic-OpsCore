"""
High-level memory API used by skills and the orchestration layer.

remember()         → save to SQLite + embed + sync to vault markdown
retrieve_context() → semantic search → return full Memory objects
"""
from __future__ import annotations

from pathlib import Path

from agent.config import settings
from agent.core.models import Memory
from agent.memory.embeddings import add_embedding, search_similar
from agent.memory.store import get_by_source, get_recent, save_memory
from agent.observability.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def remember(
    content: str,
    source: str = "agent",
    metadata: dict | None = None,
) -> Memory:
    """
    Persist a memory through the full stack:
      1. Save row to SQLite (store.py)
      2. Embed and index in ChromaDB (embeddings.py)
      3. Append to data/vault/{source}.md for human-readable audit
    """
    meta = metadata or {}

    memory = save_memory(content=content, source=source, metadata=meta)
    add_embedding(
        memory_id=memory.id,
        content=content,
        metadata={"source": source, **meta},
    )
    _sync_to_vault(memory)

    log.info("memory.retrieval.remembered", id=memory.id, source=source)
    return memory


def get_memories_by_source(source: str) -> list[Memory]:
    """Return all memories saved with the given source tag, newest first."""
    return get_by_source(source)


def retrieve_context(query: str, limit: int = 5) -> list[Memory]:
    """
    Semantic search: find the *limit* most relevant memories for *query*.

    Steps:
      1. Search ChromaDB embeddings for similar content → get IDs
      2. Load the recent memories from SQLite
      3. Return the intersection, ordered by similarity score

    Falls back to most-recent memories if the vector store is empty.
    """
    hits = search_similar(query, limit=limit)

    if not hits:
        log.debug("memory.retrieval.fallback_to_recent", query=query[:60])
        return get_recent(limit=limit)

    # Build a relevance-ordered ID list from the embedding search.
    hit_ids = {h["id"]: h["distance"] for h in hits}

    # Pull recent memories and filter to only those returned by the search.
    # We pull more than needed so we can always fill the limit even if some
    # IDs have already been deleted from SQLite.
    recent = get_recent(limit=200)
    matched = [m for m in recent if m.id in hit_ids]

    # Re-order by embedding distance (lowest distance = most similar).
    matched.sort(key=lambda m: hit_ids[m.id])

    result = matched[:limit]
    log.info(
        "memory.retrieval.retrieved",
        query=query[:60],
        hits=len(result),
    )
    return result


# ---------------------------------------------------------------------------
# Vault sync (private)
# ---------------------------------------------------------------------------

def _sync_to_vault(memory: Memory) -> None:
    """
    Append the memory to data/vault/{source}.md.

    Format:
        ## 2026-06-05 17:42:00 UTC  [id: abc123]
        Content goes here.

        ---
    """
    vault_dir = Path(settings.vault_path)
    vault_dir.mkdir(parents=True, exist_ok=True)

    vault_file = vault_dir / f"{memory.source}.md"

    # Create file with a header if it doesn't exist yet.
    if not vault_file.exists():
        vault_file.write_text(
            f"# Memory vault — source: {memory.source}\n\n",
            encoding="utf-8",
        )

    ts = memory.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = f"## {ts}  [id: {memory.id[:8]}]\n{memory.content}\n\n---\n\n"
    with vault_file.open("a", encoding="utf-8") as f:
        f.write(entry)

    log.debug("memory.vault.synced", file=str(vault_file), id=memory.id[:8])
