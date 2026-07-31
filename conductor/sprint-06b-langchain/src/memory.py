"""
Memory layer for Conductor -- Sprint 6.

Sprint 5 ran a three-provider benchmark:
  Redis   10ms  keyword-only, no semantic ranking
  Qdrant  82ms  semantic, matched 30.8% baseline, lowest overhead of semantic providers
  Mem0  1035ms  LLM extraction -- overhead not worth paying without a real KB

Decision: Qdrant only from Sprint 6 forward. Redis memory provider and Mem0 dropped.
Redis session cache (SessionStore, Layer 1) is unchanged -- separate role.

Mem0+Qdrant (extraction layer) deferred to Lab 7b where real KB content makes
extraction quality measurable.

Every add/search/delete requires user_id (RULE-MEM01).
Memory is never auto-injected -- the agent retrieves it explicitly via tool call (RULE-MEM02).
"""

import logging
import os
import time
import uuid
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol -- the only interface callers may use (RULE-MEM03)
# ---------------------------------------------------------------------------

@runtime_checkable
class MemoryStore(Protocol):
    """
    Unified interface for all memory providers.
    user_id is required on every call -- enforces namespace isolation (RULE-MEM01).
    """

    @property
    def provider_name(self) -> str: ...

    def add(self, content: str, user_id: str, metadata: dict | None = None) -> str:
        """Store a memory entry. Returns the stored entry id."""
        ...

    def search(self, query: str, user_id: str, limit: int = 5) -> list[dict]:
        """
        Retrieve relevant memories for this user.
        Returns list of dicts with keys: id, content, metadata, score (0-1).
        """
        ...

    def delete(self, memory_id: str, user_id: str) -> bool:
        """Delete a specific memory entry. Returns True if deleted."""
        ...

    def get_all(self, user_id: str) -> list[dict]:
        """Return all memory entries for this user (for inspection/tests)."""
        ...


# ---------------------------------------------------------------------------
# Provider -- Qdrant episodic/semantic store
# ---------------------------------------------------------------------------

class QdrantMemoryStore:
    """
    Stores episodic session narratives as vector embeddings in Qdrant.
    Collection: conductor_memory
    Payload filter on user_id enforces namespace isolation (RULE-MEM01).
    Embeddings via fastembed (local, no API key needed).
    """

    provider_name = "qdrant"
    COLLECTION = "conductor_memory"
    VECTOR_SIZE = 384  # all-MiniLM-L6-v2 via fastembed

    def __init__(self, host: str = "localhost", port: int = 6333):
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams, PayloadSchemaType
        self._client = QdrantClient(host=host, port=port)
        self._embedding_model = None  # lazy-loaded on first embed
        existing = [c.name for c in self._client.get_collections().collections]
        if self.COLLECTION not in existing:
            self._client.create_collection(
                collection_name=self.COLLECTION,
                vectors_config=VectorParams(size=self.VECTOR_SIZE, distance=Distance.COSINE),
            )
            self._client.create_payload_index(
                collection_name=self.COLLECTION,
                field_name="user_id",
                field_schema=PayloadSchemaType.KEYWORD,
            )

    def _embed(self, text: str) -> list[float]:
        if self._embedding_model is None:
            from fastembed import TextEmbedding
            self._embedding_model = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")
        return list(next(self._embedding_model.embed([text])))

    def add(self, content: str, user_id: str, metadata: dict | None = None) -> str:
        from qdrant_client.models import PointStruct
        entry_id = str(uuid.uuid4())
        vector = self._embed(content)
        payload = {
            "content": content,
            "user_id": user_id,
            "timestamp": time.time(),
            "metadata": metadata or {},
        }
        self._client.upsert(
            collection_name=self.COLLECTION,
            points=[PointStruct(id=entry_id, vector=vector, payload=payload)],
        )
        return entry_id

    def search(self, query: str, user_id: str, limit: int = 5) -> list[dict]:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        vector = self._embed(query)
        result = self._client.query_points(
            collection_name=self.COLLECTION,
            query=vector,
            query_filter=Filter(
                must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))]
            ),
            limit=limit,
            with_payload=True,
        )
        return [
            {
                "id": str(hit.id),
                "content": hit.payload.get("content", ""),
                "metadata": hit.payload.get("metadata", {}),
                "score": round(hit.score, 3),
                "timestamp": hit.payload.get("timestamp"),
            }
            for hit in result.points
        ]

    def delete(self, memory_id: str, user_id: str) -> bool:
        from qdrant_client.models import Filter, FieldCondition, MatchValue, PointIdsList
        # Verify ownership before deleting
        results = self.search(memory_id, user_id=user_id, limit=1)
        found = any(r["id"] == memory_id for r in results)
        if not found:
            return False
        self._client.delete(
            collection_name=self.COLLECTION,
            points_selector=PointIdsList(points=[memory_id]),
        )
        return True

    def get_all(self, user_id: str) -> list[dict]:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        results, _ = self._client.scroll(
            collection_name=self.COLLECTION,
            scroll_filter=Filter(
                must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))]
            ),
            with_payload=True,
            limit=100,
        )
        return [
            {
                "id": str(p.id),
                "content": p.payload.get("content", ""),
                "metadata": p.payload.get("metadata", {}),
            }
            for p in results
        ]


# ---------------------------------------------------------------------------
# In-memory stub -- for CI / tests without running infrastructure
# ---------------------------------------------------------------------------

class InMemoryStore:
    """CI-only. No persistence, no semantic ranking."""

    provider_name = "inmemory"

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    def add(self, content: str, user_id: str, metadata: dict | None = None) -> str:
        entry_id = str(uuid.uuid4())
        self._store[entry_id] = {
            "content": content,
            "user_id": user_id,
            "metadata": metadata or {},
        }
        return entry_id

    def search(self, query: str, user_id: str, limit: int = 5) -> list[dict]:
        q_lower = query.lower()
        matches = [
            {"id": k, "content": v["content"], "metadata": v["metadata"], "score": 0.5, "timestamp": None}
            for k, v in self._store.items()
            if v["user_id"] == user_id and q_lower in v["content"].lower()
        ]
        return matches[:limit]

    def delete(self, memory_id: str, user_id: str) -> bool:
        entry = self._store.get(memory_id)
        if entry is None or entry["user_id"] != user_id:
            return False
        del self._store[memory_id]
        return True

    def get_all(self, user_id: str) -> list[dict]:
        return [
            {"id": k, "content": v["content"], "metadata": v["metadata"]}
            for k, v in self._store.items()
            if v["user_id"] == user_id
        ]


# ---------------------------------------------------------------------------
# Factory -- provider selection via env var (RULE-MEM03)
# ---------------------------------------------------------------------------

def make_memory_store(provider: str | None = None) -> MemoryStore:
    """
    Build a MemoryStore from the MEMORY_PROVIDER env var or the provider argument.
    Caller never imports a concrete class -- always uses the MemoryStore protocol.
    Supported: qdrant | inmemory (only). Redis and Mem0 removed in Sprint 6.
    """
    name = (provider or os.environ.get("MEMORY_PROVIDER", "inmemory")).lower()

    if name == "qdrant":
        host = os.environ.get("QDRANT_HOST", "localhost")
        port = int(os.environ.get("QDRANT_PORT", "6333"))
        return QdrantMemoryStore(host=host, port=port)

    if name == "inmemory":
        return InMemoryStore()

    raise ValueError(
        f"Unknown MEMORY_PROVIDER: {name!r}. "
        "Use qdrant | inmemory. (Redis and Mem0 removed in Sprint 6 -- see docs/decisions/memory-sprint6.md)"
    )
