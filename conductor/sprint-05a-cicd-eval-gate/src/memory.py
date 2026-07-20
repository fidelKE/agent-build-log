"""
Memory layer for Conductor — Sprint 4.

Three providers behind one MemoryStore protocol. Switching providers is a
constructor argument — no changes required in callers (RULE-MEM03).

Provider selection via MEMORY_PROVIDER env var:
  redis   → RedisMemoryStore  (K/V entity facts, exact lookup, no embeddings)
  qdrant  → QdrantMemoryStore (episodic/semantic, vector search via fastembed)
  mem0    → Mem0MemoryStore   (hybrid abstraction, Mem0 decides what to extract)

Every add/search call requires user_id (RULE-MEM01).
Memory is never auto-injected — the agent retrieves it explicitly via tool call (RULE-MEM02).

Mode mapping (from docs/decisions/memory-mode-mapping.md):
  Troubleshooting  → Qdrant (episodic) + Redis (K/V entity)
  Onboarding       → Redis K/V entity
  Setup            → Redis K/V entity
  Knowledge Q&A    → no memory (fresh lookup preferred)
"""

import json
import logging
import os
import time
import uuid
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol — the only interface callers may use (RULE-MEM03)
# ---------------------------------------------------------------------------

@runtime_checkable
class MemoryStore(Protocol):
    """
    Unified interface for all memory providers.
    user_id is required on every call — enforces namespace isolation (RULE-MEM01).
    """

    @property
    def provider_name(self) -> str: ...

    def add(self, content: str, user_id: str, metadata: dict | None = None) -> str:
        """Store a memory entry. Returns the stored key or entry id."""
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
# Provider 1 — Redis K/V entity store
# ---------------------------------------------------------------------------

class RedisMemoryStore:
    """
    Stores structured entity facts as Redis hashes.
    Key pattern: mem:entity:{user_id}:{entry_id}
    Scan pattern: mem:entity:{user_id}:*

    No embeddings. Retrieval is keyword substring match — deterministic.
    Best for: structured facts (connector type, error codes, steps tried, preferences).
    Limitation: no semantic similarity — "auth failure" won't match "authentication error".
    """

    provider_name = "redis"

    def __init__(self, host: str = "localhost", port: int = 6379, db: int = 1):
        import redis as redis_lib
        self._client = redis_lib.Redis(host=host, port=port, db=db,
                                       decode_responses=True,
                                       socket_connect_timeout=3)

    def _key(self, user_id: str, entry_id: str) -> str:
        return f"mem:entity:{user_id}:{entry_id}"

    def _scan_pattern(self, user_id: str) -> str:
        return f"mem:entity:{user_id}:*"

    def add(self, content: str, user_id: str, metadata: dict | None = None) -> str:
        entry_id = str(uuid.uuid4())[:8]
        key = self._key(user_id, entry_id)
        payload = {
            "content": content,
            "user_id": user_id,
            "timestamp": str(time.time()),
            "metadata": json.dumps(metadata or {}),
        }
        self._client.hset(key, mapping=payload)
        return entry_id

    def search(self, query: str, user_id: str, limit: int = 5) -> list[dict]:
        pattern = self._scan_pattern(user_id)
        keys = list(self._client.scan_iter(pattern))
        results = []
        query_lower = query.lower()
        for key in keys:
            entry = self._client.hgetall(key)
            if not entry:
                continue
            content = entry.get("content", "")
            # Keyword substring match — score by fraction of query words matched
            words = query_lower.split()
            matched = sum(1 for w in words if w in content.lower())
            score = matched / max(len(words), 1) if words else 0.0
            if score > 0:
                results.append({
                    "id": key.split(":")[-1],
                    "content": content,
                    "metadata": json.loads(entry.get("metadata", "{}")),
                    "score": round(score, 3),
                    "timestamp": entry.get("timestamp"),
                })
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    def delete(self, memory_id: str, user_id: str) -> bool:
        key = self._key(user_id, memory_id)
        return bool(self._client.delete(key))

    def get_all(self, user_id: str) -> list[dict]:
        pattern = self._scan_pattern(user_id)
        keys = list(self._client.scan_iter(pattern))
        results = []
        for key in keys:
            entry = self._client.hgetall(key)
            if entry:
                results.append({
                    "id": key.split(":")[-1],
                    "content": entry.get("content", ""),
                    "metadata": json.loads(entry.get("metadata", "{}")),
                    "score": 1.0,
                    "timestamp": entry.get("timestamp"),
                })
        return results


# ---------------------------------------------------------------------------
# Provider 2 — Qdrant episodic/semantic store
# ---------------------------------------------------------------------------

class QdrantMemoryStore:
    """
    Stores episodic session narratives as vector embeddings in Qdrant.
    Collection: conductor_memory
    Payload filter on user_id enforces namespace isolation.

    Embeddings via fastembed (local, no API key needed).
    Best for: "find past sessions where this user had auth failures" — semantic search
    over narrative text even when the exact wording differs.
    """

    provider_name = "qdrant"
    COLLECTION = "conductor_memory"
    VECTOR_SIZE = 384  # nomic-embed-text-v1.5 / all-MiniLM-L6-v2 via fastembed

    def __init__(self, host: str = "localhost", port: int = 6333):
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams, PayloadSchemaType
        from qdrant_client.models import CreateCollection

        self._client = QdrantClient(host=host, port=port)
        self._embedding_model = None  # lazy-loaded on first use

        # Create collection if it doesn't exist
        existing = [c.name for c in self._client.get_collections().collections]
        if self.COLLECTION not in existing:
            self._client.create_collection(
                collection_name=self.COLLECTION,
                vectors_config=VectorParams(size=self.VECTOR_SIZE, distance=Distance.COSINE),
            )
            # Index user_id for fast filtered search
            self._client.create_payload_index(
                collection_name=self.COLLECTION,
                field_name="user_id",
                field_schema=PayloadSchemaType.KEYWORD,
            )

    def _embed(self, text: str) -> list[float]:
        if self._embedding_model is None:
            from fastembed import TextEmbedding
            self._embedding_model = TextEmbedding("sentence-transformers/all-MiniLM-L6-v2")
        embeddings = list(self._embedding_model.embed([text]))
        return embeddings[0].tolist()

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
        # qdrant-client v1.12+ replaced .search() with .query_points()
        result = self._client.query_points(
            collection_name=self.COLLECTION,
            query=vector,
            query_filter=Filter(
                must=[FieldCondition(key="user_id", match=MatchValue(value=user_id))]
            ),
            limit=limit,
            with_payload=True,
            score_threshold=0.1,  # drop near-zero similarity results
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
        results = self._client.retrieve(
            collection_name=self.COLLECTION,
            ids=[memory_id],
            with_payload=True,
        )
        if not results or results[0].payload.get("user_id") != user_id:
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
            limit=100,
            with_payload=True,
        )
        return [
            {
                "id": str(r.id),
                "content": r.payload.get("content", ""),
                "metadata": r.payload.get("metadata", {}),
                "score": 1.0,
                "timestamp": r.payload.get("timestamp"),
            }
            for r in results
        ]


# ---------------------------------------------------------------------------
# Provider 3 — Mem0 hybrid abstraction
# ---------------------------------------------------------------------------

class Mem0MemoryStore:
    """
    Wraps the mem0ai Python library. Mem0 decides what to extract from content,
    how to chunk it, and when to merge vs. append facts. The agent uses the same
    add/search interface — Mem0 handles the rest.

    Requires: mem0ai[extras] for fastembed support (avoids OpenAI embedding dependency).
    Falls back to OpenAI-compatible embedding if LLM_GATEWAY_URL is set.

    Best for: comparing managed extraction quality vs. manually structured Redis entries.
    The key question: does Mem0's auto-extraction produce better retrieval than explicit K/V?
    """

    provider_name = "mem0"

    def __init__(self, qdrant_host: str = "localhost", qdrant_port: int = 6333):
        from mem0 import Memory

        gateway_url = os.environ.get("LLM_GATEWAY_URL", "")
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")

        config = {
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "host": qdrant_host,
                    "port": qdrant_port,
                    "collection_name": "conductor_mem0",
                },
            },
            "embedder": {
                "provider": "openai",  # text-embedding-3-small via gateway (same as server)
                "config": {
                    "model": "text-embedding-3-small",
                    "openai_base_url": gateway_url,
                    "api_key": api_key,
                },
            },
            "llm": {
                "provider": "openai",  # gateway is OpenAI-compatible
                "config": {
                    # claude-haiku-4-5 rejected by Bedrock (temperature+top_p conflict)
                    "model": os.environ.get("MEM0_LLM_MODEL", "gpt-4.1-mini"),
                    "openai_base_url": gateway_url,
                    "api_key": api_key,
                },
            },
        }
        self._mem = Memory.from_config(config)

    def add(self, content: str, user_id: str, metadata: dict | None = None) -> str:
        result = self._mem.add(content, user_id=user_id, metadata=metadata or {})
        # mem0 returns {"results": [{"id": ..., "memory": ..., "event": ...}]}
        results = result.get("results", [])
        if results:
            return results[0].get("id", "unknown")
        return "unknown"

    def search(self, query: str, user_id: str, limit: int = 5) -> list[dict]:
        results = self._mem.search(query, filters={"user_id": user_id}, limit=limit)
        memories = results.get("results", [])
        return [
            {
                "id": m.get("id", ""),
                "content": m.get("memory", ""),
                "metadata": m.get("metadata", {}),
                "score": round(m.get("score", 0.0), 3),
                "timestamp": None,
            }
            for m in memories
        ]

    def delete(self, memory_id: str, user_id: str) -> bool:
        try:
            self._mem.delete(memory_id)
            return True
        except Exception:
            return False

    def get_all(self, user_id: str) -> list[dict]:
        results = self._mem.get_all(filters={"user_id": user_id})
        memories = results.get("results", [])
        return [
            {
                "id": m.get("id", ""),
                "content": m.get("memory", ""),
                "metadata": m.get("metadata", {}),
                "score": 1.0,
                "timestamp": None,
            }
            for m in memories
        ]


# ---------------------------------------------------------------------------
# Provider 3b — Mem0 server mode (self-hosted REST API)
# ---------------------------------------------------------------------------

class Mem0ServerStore:
    """
    HTTP client for the self-hosted Mem0 server (server/docker-compose.yaml).
    Same MemoryStore interface as Mem0MemoryStore — the difference is deployment:

      Mem0MemoryStore    — in-process library, wraps Qdrant directly
      Mem0ServerStore    — HTTP client, calls POST /memories + POST /search

    API:
      POST /memories       body: {messages:[{role,content}], user_id, metadata}
      POST /search         body: {query, filters:{user_id}, top_k}
      DELETE /memories/:id path param
      GET  /memories       query: user_id=
    Auth: X-API-Key header

    To start the server:
      cd conductor/repos/mem0
      make bootstrap   (prints API key on first run)
    Then set MEM0_SERVER_URL and MEM0_API_KEY in .env.

    Not used in sprint 4 benchmarks — extraction behavior is identical to
    Mem0MemoryStore (same pipeline, just over HTTP). Kept because the server
    deployment becomes relevant in Sprint 9/10 when multiple agent instances
    need to share a single memory service across tenants.
    """

    provider_name = "mem0-server"

    def __init__(self, base_url: str = "http://localhost:8888", api_key: str = ""):
        import httpx
        self._base = base_url.rstrip("/")
        self._headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
        self._client = httpx.Client(timeout=15.0)

    def add(self, content: str, user_id: str, metadata: dict | None = None) -> str:
        import httpx
        payload = {
            "messages": [{"role": "user", "content": content}],
            "user_id": user_id,
            "metadata": metadata or {},
        }
        try:
            r = self._client.post(f"{self._base}/memories",
                                  headers=self._headers, json=payload)
            r.raise_for_status()
            data = r.json()
            results = data.get("results", [])
            if results and isinstance(results[0], dict):
                return results[0].get("id", "unknown")
            return "unknown"
        except httpx.HTTPError as e:
            logger.error("Mem0ServerStore.add failed: %s", e)
            raise

    def search(self, query: str, user_id: str, limit: int = 5) -> list[dict]:
        import httpx
        payload = {
            "query": query,
            "filters": {"user_id": user_id},
            "top_k": limit,
        }
        try:
            r = self._client.post(f"{self._base}/search",
                                  headers=self._headers, json=payload)
            r.raise_for_status()
            data = r.json()
            results = data.get("results", [])
            return [
                {
                    "id": m.get("id", ""),
                    "content": m.get("memory", ""),
                    "metadata": m.get("metadata", {}),
                    "score": round(m.get("score", 0.0), 3),
                    "timestamp": None,
                }
                for m in results
            ]
        except httpx.HTTPError as e:
            logger.error("Mem0ServerStore.search failed: %s", e)
            raise

    def delete(self, memory_id: str, user_id: str) -> bool:
        import httpx
        try:
            r = self._client.delete(f"{self._base}/memories/{memory_id}",
                                    headers=self._headers)
            return r.status_code in (200, 204)
        except httpx.HTTPError:
            return False

    def get_all(self, user_id: str) -> list[dict]:
        import httpx
        try:
            r = self._client.get(f"{self._base}/memories",
                                 headers=self._headers,
                                 params={"user_id": user_id})
            r.raise_for_status()
            data = r.json()
            results = data.get("results", [])
            return [
                {
                    "id": m.get("id", ""),
                    "content": m.get("memory", ""),
                    "metadata": m.get("metadata", {}),
                    "score": 1.0,
                    "timestamp": None,
                }
                for m in results
            ]
        except httpx.HTTPError as e:
            logger.error("Mem0ServerStore.get_all failed: %s", e)
            return []


# ---------------------------------------------------------------------------
# In-memory stub — for CI / tests without running infrastructure
# ---------------------------------------------------------------------------

class InMemoryStore:
    """
    No-infra stub used in CI and unit tests. Namespace-isolated by user_id.
    Keyword match only — same as Redis but backed by a plain dict.
    """

    provider_name = "inmemory"

    def __init__(self) -> None:
        self._store: dict[str, list[dict]] = {}

    def add(self, content: str, user_id: str, metadata: dict | None = None) -> str:
        entry_id = str(uuid.uuid4())[:8]
        self._store.setdefault(user_id, []).append({
            "id": entry_id,
            "content": content,
            "user_id": user_id,
            "metadata": metadata or {},
            "timestamp": time.time(),
        })
        return entry_id

    def search(self, query: str, user_id: str, limit: int = 5) -> list[dict]:
        entries = self._store.get(user_id, [])
        query_lower = query.lower()
        words = query_lower.split()
        scored = []
        for entry in entries:
            matched = sum(1 for w in words if w in entry["content"].lower())
            score = matched / max(len(words), 1) if words else 0.0
            if score > 0:
                scored.append({**entry, "score": round(score, 3)})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    def delete(self, memory_id: str, user_id: str) -> bool:
        entries = self._store.get(user_id, [])
        before = len(entries)
        self._store[user_id] = [e for e in entries if e["id"] != memory_id]
        return len(self._store[user_id]) < before

    def get_all(self, user_id: str) -> list[dict]:
        return list(self._store.get(user_id, []))


# ---------------------------------------------------------------------------
# Factory — provider selection via env var (RULE-MEM03)
# ---------------------------------------------------------------------------

def make_memory_store(provider: str | None = None) -> MemoryStore:
    """
    Build a MemoryStore from the MEMORY_PROVIDER env var or the provider argument.
    Caller never imports a concrete class — always uses the MemoryStore protocol.
    """
    name = (provider or os.environ.get("MEMORY_PROVIDER", "inmemory")).lower()

    if name == "redis":
        host = os.environ.get("REDIS_HOST", "localhost")
        port = int(os.environ.get("REDIS_PORT", "6379"))
        return RedisMemoryStore(host=host, port=port)

    if name == "qdrant":
        host = os.environ.get("QDRANT_HOST", "localhost")
        port = int(os.environ.get("QDRANT_PORT", "6333"))
        return QdrantMemoryStore(host=host, port=port)

    if name == "mem0":
        host = os.environ.get("QDRANT_HOST", "localhost")
        port = int(os.environ.get("QDRANT_PORT", "6333"))
        return Mem0MemoryStore(qdrant_host=host, qdrant_port=port)

    if name == "mem0-server":
        url = os.environ.get("MEM0_SERVER_URL", "http://localhost:8888")
        api_key = os.environ.get("MEM0_API_KEY", "")
        return Mem0ServerStore(base_url=url, api_key=api_key)

    if name == "inmemory":
        return InMemoryStore()

    raise ValueError(
        f"Unknown MEMORY_PROVIDER: {name!r}. "
        "Use redis | qdrant | mem0 | mem0-server | inmemory"
    )


# ---------------------------------------------------------------------------
# SummaryMemory — compression wrapper (A8, §11.2)
# ---------------------------------------------------------------------------

class SummaryMemory:
    """
    Wraps any MemoryStore and compresses old entries when the total token count
    exceeds compression_threshold.

    When compression fires:
      1. Retrieve all entries for the user.
      2. Take the oldest N entries (those not yet compressed).
      3. Call summarize_fn(entries) → one compressed string.
      4. Delete the N original entries.
      5. Store the summary as a single entry tagged type="summary".

    O(1) context cost regardless of history length — the tradeoff is lossy compression:
    exact error codes, timestamps, and specific steps tried are lost in the summary.
    Compressed entries are still retrievable via search; keyword match works on the
    summary text.

    Token counting: each entry's content is counted as len(content) // 4 (approx tokens).
    This is a rough proxy — for production use tiktoken or the actual encoder.

    Args:
        store: underlying MemoryStore provider
        compression_threshold: total token count that triggers compression (default: 2000)
        compress_oldest_n: how many entries to fold into one summary (default: 5)
        summarize_fn: callable(entries: list[dict]) -> str; defaults to naive join
    """

    provider_name = "summary"

    def __init__(
        self,
        store: MemoryStore,
        compression_threshold: int = 2000,
        compress_oldest_n: int = 5,
        summarize_fn=None,
    ):
        self._store = store
        self.compression_threshold = compression_threshold
        self.compress_oldest_n = compress_oldest_n
        self._summarize = summarize_fn or _default_summarize

    @staticmethod
    def _count_tokens(entries: list[dict]) -> int:
        return sum(len(e.get("content", "")) // 4 for e in entries)

    def _maybe_compress(self, user_id: str) -> int:
        """Compress if over threshold. Returns number of entries folded (0 if no compression)."""
        all_entries = self._store.get_all(user_id)
        total_tokens = self._count_tokens(all_entries)
        if total_tokens <= self.compression_threshold:
            return 0

        # Sort by timestamp ascending (oldest first); entries without timestamps go last
        sortable = sorted(
            all_entries,
            key=lambda e: float(e.get("timestamp") or 0),
        )

        # Only compress non-summary entries to avoid re-compressing summaries
        non_summary = [e for e in sortable if e.get("metadata", {}).get("type") != "summary"]
        to_fold = non_summary[: self.compress_oldest_n]
        if not to_fold:
            return 0

        # Build summary and store it
        summary_text = self._summarize(to_fold)
        self._store.add(
            summary_text,
            user_id=user_id,
            metadata={"type": "summary", "folded_count": len(to_fold)},
        )

        # Delete folded entries
        for entry in to_fold:
            self._store.delete(entry["id"], user_id)

        logger.info(
            "SummaryMemory: compressed %d entries into 1 summary for user %s "
            "(was %d tokens, threshold %d)",
            len(to_fold),
            user_id,
            total_tokens,
            self.compression_threshold,
        )
        return len(to_fold)

    def add(self, content: str, user_id: str, metadata: dict | None = None) -> str:
        entry_id = self._store.add(content, user_id, metadata)
        self._maybe_compress(user_id)
        return entry_id

    def search(self, query: str, user_id: str, limit: int = 5) -> list[dict]:
        return self._store.search(query, user_id, limit)

    def delete(self, memory_id: str, user_id: str) -> bool:
        return self._store.delete(memory_id, user_id)

    def get_all(self, user_id: str) -> list[dict]:
        return self._store.get_all(user_id)


def _default_summarize(entries: list[dict]) -> str:
    """Naive compressing summarizer: truncate each entry to 40 chars and join.

    The truncation is what makes this compressing: N entries of ~200 chars each
    become one entry of N * ~40 chars. Without truncation, joining re-emits the
    full content and total tokens can increase. Real deployments should pass an
    LLM-backed summarize_fn to SummaryMemory instead.
    """
    snippets = [e["content"][:40].rstrip() for e in entries]
    return f"[summary/{len(entries)}] " + " | ".join(snippets)
