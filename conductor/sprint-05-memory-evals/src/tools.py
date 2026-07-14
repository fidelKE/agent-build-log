"""
Tool layer for Conductor — Sprint 4.

Sprint 1: notes_search — in-memory knowledge base, Pydantic-validated, no auth.
Sprint 3: search_knowledge_base — real REST API, credential injection via ToolExecutor.
Sprint 4: search_memory / add_memory — tool-based memory retrieval and storage.
          The agent calls these explicitly — memory is never auto-injected (RULE-MEM02).
          user_id is required on every call (RULE-MEM01).

Conductor modes:
  notes_search            → Q&A (local KB, no network, no credentials needed)
  search_knowledge_base   → Q&A + Troubleshooting (live catalog, credential injection)
  search_memory           → Troubleshooting + Onboarding + Setup (cross-session recall)
  add_memory              → Troubleshooting + Onboarding + Setup (persist session facts)
"""

import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field, field_validator

from .memory import MemoryStore
from .secrets import SecretStore, redact

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sprint 1 — Error contract + notes_search (in-memory KB)
# ---------------------------------------------------------------------------

class ToolError(BaseModel):
    error_code: str
    message: str
    retryable: bool
    retry_after: int | None = None  # seconds to wait before retrying; None = no guidance

    def to_dict(self) -> dict:
        result = {"error": True, "error_code": self.error_code,
                  "message": self.message, "retryable": self.retryable}
        if self.retry_after is not None:
            result["retry_after"] = self.retry_after
        return result


class NotesSearchInput(BaseModel):
    query: str = Field(..., min_length=1, max_length=500,
                       description="Search query to find relevant notes")
    max_results: int = Field(default=3, ge=1, le=10,
                             description="Maximum number of notes to return")

    @field_validator("query")
    @classmethod
    def query_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query must not be blank")
        return v.strip()


class NoteResult(BaseModel):
    id: str
    title: str
    snippet: str
    score: float


class NotesSearchOutput(BaseModel):
    results: list[NoteResult]
    total_found: int


_NOTES: list[dict] = [
    {
        "id": "note-001",
        "title": "Snowflake connector setup",
        "body": "To connect Snowflake: provide account identifier, warehouse, database, schema, username, and password or private key. Use IAM role for production environments.",
    },
    {
        "id": "note-002",
        "title": "Troubleshooting connection timeouts",
        "body": "Connection timeouts usually indicate a firewall or VPC rule blocking outbound traffic. Check security group rules and ensure the source IP is allowlisted.",
    },
    {
        "id": "note-003",
        "title": "BigQuery authentication",
        "body": "BigQuery uses service account JSON keys or workload identity. Never embed the key in code — inject it via environment variable at runtime.",
    },
    {
        "id": "note-004",
        "title": "dbt lineage extraction",
        "body": "To extract dbt lineage, point the connector at the dbt project directory or manifest.json. The parser reads sources, models, and exposures automatically.",
    },
    {
        "id": "note-005",
        "title": "First-time onboarding checklist",
        "body": "Step 1: create a service account with read-only access. Step 2: generate credentials. Step 3: run a connection test. Step 4: confirm schema detection.",
    },
]


def _score(note: dict, query: str) -> float:
    query_terms = set(query.lower().split())
    text = (note["title"] + " " + note["body"]).lower()
    matches = sum(1 for term in query_terms if term in text)
    return matches / max(len(query_terms), 1)


def notes_search(raw_input: dict) -> dict:
    """
    Search the local integration knowledge base.

    Use when: answering 'how do I...', 'what is...', or troubleshooting questions.
    Do NOT use for: creating notes, modifying connector config, or executing actions.
    """
    try:
        args = NotesSearchInput.model_validate(raw_input)
    except Exception as exc:
        return ToolError(error_code="INVALID_INPUT", message=str(exc), retryable=False).to_dict()

    scored = sorted([(note, _score(note, args.query)) for note in _NOTES],
                    key=lambda x: x[1], reverse=True)
    top = [note for note, score in scored[:args.max_results] if score > 0]
    results = [
        NoteResult(id=n["id"], title=n["title"],
                   snippet=n["body"][:200], score=round(_score(n, args.query), 3))
        for n in top
    ]
    return NotesSearchOutput(results=results, total_found=len(top)).model_dump()


# ---------------------------------------------------------------------------
# Sprint 3 — ToolExecutor with credential injection (search_knowledge_base)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Sprint 4 — Memory tool Pydantic models (RULE-T01/T02/T03, RULE-MEM01)
# ---------------------------------------------------------------------------

class SearchMemoryInput(BaseModel):
    query: str = Field(..., min_length=1, max_length=500,
                       description="What to search for in memory")
    user_id: str = Field(..., min_length=1, max_length=128,
                         description="User identifier for namespace isolation")
    limit: int = Field(default=5, ge=1, le=20,
                       description="Maximum number of memories to return")

    @field_validator("query", "user_id")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("field must not be blank")
        return v.strip()


class MemoryResult(BaseModel):
    id: str
    content: str
    score: float
    metadata: dict


class SearchMemoryOutput(BaseModel):
    results: list[MemoryResult]
    total_found: int
    provider: str


class AddMemoryInput(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000,
                         description="The memory to store")
    user_id: str = Field(..., min_length=1, max_length=128,
                         description="User identifier for namespace isolation")
    metadata: dict = Field(default_factory=dict,
                           description="Optional structured metadata (mode, connector, etc.)")

    @field_validator("content", "user_id")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("field must not be blank")
        return v.strip()


class AddMemoryOutput(BaseModel):
    stored_id: str
    provider: str


class DeleteMemoryInput(BaseModel):
    memory_id: str = Field(..., min_length=1, max_length=256,
                           description="ID of the memory to delete (from search_memory results)")
    user_id: str = Field(..., min_length=1, max_length=128,
                         description="User identifier — must match the memory owner")

    @field_validator("memory_id", "user_id")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("field must not be blank")
        return v.strip()


class DeleteMemoryOutput(BaseModel):
    deleted: bool
    memory_id: str
    provider: str


# Tool schemas exposed to the model. No auth fields anywhere — intentional.
TOOL_SCHEMAS = [
    {
        "name": "notes_search",
        "version": "1.0",
        "description": (
            "Search the local integration knowledge base for setup guides, "
            "troubleshooting steps, and how-to notes. "
            "Use when the user asks 'how do I...', 'what is...', or reports an error. "
            "Do NOT use for creating notes, modifying connector config, or executing actions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (1-500 chars)"},
                "max_results": {"type": "integer", "description": "Max notes to return (1-10, default 3)", "default": 3},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_knowledge_base",
        "version": "1.0",
        "description": (
            "Search the live data catalog for assets, connectors, and integration metadata. "
            "Use when the user asks about specific datasets, tables, or connector configurations "
            "that may be in the catalog. Complements notes_search with live data."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query. Be specific about the integration or asset."},
                "max_results": {"type": "integer", "description": "Maximum number of results to return (1-10). Default: 3.", "default": 3},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_memory",
        "version": "1.0",
        "description": (
            "Retrieve memories from past sessions for this user. "
            "Use when the user references a previous issue, prior setup steps, or past interactions "
            "that may provide useful context. Always call this before asking the user to repeat "
            "information they may have already provided in a previous session. "
            "Do NOT use for Knowledge Q&A mode — fresh lookup is preferred there."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for in past sessions (1-500 chars)"},
                "user_id": {"type": "string", "description": "The current user's identifier"},
                "limit": {"type": "integer", "description": "Maximum memories to return (1-20, default 5)", "default": 5},
            },
            "required": ["query", "user_id"],
        },
    },
    {
        "name": "add_memory",
        "version": "1.0",
        "description": (
            "Save an important fact or session summary to memory for future sessions. "
            "Use at the end of a Troubleshooting, Onboarding, or Setup interaction to persist "
            "key facts: connector type, error codes, steps tried, user preferences. "
            "Keep content concise and factual — this will be retrieved in future sessions. "
            "Do NOT use for Knowledge Q&A mode."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The memory to store (1-2000 chars)"},
                "user_id": {"type": "string", "description": "The current user's identifier"},
                "metadata": {
                    "type": "object",
                    "description": "Optional structured metadata: mode, connector, error_code, etc.",
                },
            },
            "required": ["content", "user_id"],
        },
    },
    {
        "name": "delete_memory",
        "version": "1.0",
        "description": (
            "Delete a specific memory entry that is outdated or incorrect. "
            "Only call this when the user explicitly asks to remove or correct a stored memory. "
            "Never call autonomously — always require explicit user instruction before deleting. "
            "Use search_memory first to find the memory_id, then call this to remove it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "ID of the memory to delete (from search_memory results)"},
                "user_id": {"type": "string", "description": "The current user's identifier"},
            },
            "required": ["memory_id", "user_id"],
        },
    },
]


# ---------------------------------------------------------------------------
# Sprint 3 — Pydantic schemas for search_knowledge_base (same pattern as sprint 1)
# ---------------------------------------------------------------------------

class SearchKBInput(BaseModel):
    query: str = Field(..., min_length=1, max_length=500,
                       description="Search query to find catalog assets")
    max_results: int = Field(default=3, ge=1, le=10,
                             description="Maximum number of results to return")

    @field_validator("query")
    @classmethod
    def query_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query must not be blank")
        return v.strip()


class CatalogAsset(BaseModel):
    name: str
    type: str
    description: str
    qualified_name: str


class SearchKBOutput(BaseModel):
    results: list[CatalogAsset]
    total: int


class ToolExecutor:
    """
    Executes tool calls with credential injection for authenticated tools.

    notes_search runs locally with no credentials.
    search_knowledge_base validates input with Pydantic, fetches a short-lived
    token from the secret store at call time, and returns a typed output.
    search_memory / add_memory delegate to MemoryStore — provider selected at
    construction time, callers never interact with a concrete class (RULE-MEM03).

    Pass structured_logger to enable http_call and memory trace events.
    """

    def __init__(
        self,
        secret_store: SecretStore,
        catalog_base_url: str,
        memory_store: MemoryStore | None = None,
        secret_key: str = "catalog-api-token",
        structured_logger: Any = None,
    ):
        self._store = secret_store
        self._catalog_base_url = catalog_base_url.rstrip("/")
        self._memory = memory_store
        self._secret_key = secret_key
        self._logger = structured_logger

    def _log_http(self, step_id: str, url: str, request_body: dict,
                  status_code: int | None, response_body: Any,
                  duration_ms: float, error: str | None = None) -> None:
        """Write http_call event to StructuredLogger if available.
        Authorization header is never included — it is stripped at this layer."""
        if self._logger is None:
            return
        self._logger._write({
            "event": "http_call",
            "step_id": step_id,
            "http.url": url,
            "http.method": "POST",
            "http.request_body": request_body,
            # Authorization header intentionally absent — credential injection
            # means the token was fetched and used without entering the trace.
            "http.request_headers": {"Content-Type": "application/json"},
            "http.status_code": status_code,
            "http.response_body": response_body,
            "duration_ms": duration_ms,
            "error": error,
        })

    def execute(self, tool_name: str, tool_input: dict[str, Any],
                step_id: str = "tool") -> dict[str, Any]:
        if tool_name == "notes_search":
            return notes_search(tool_input)
        if tool_name == "search_knowledge_base":
            return self._search_knowledge_base(tool_input, step_id=step_id)
        if tool_name == "search_memory":
            return self._search_memory(tool_input, step_id=step_id)
        if tool_name == "add_memory":
            return self._add_memory(tool_input, step_id=step_id)
        if tool_name == "delete_memory":
            return self._delete_memory(tool_input, step_id=step_id)
        raise ValueError(f"Unknown tool: {tool_name}")

    def _search_memory(self, raw_input: dict[str, Any],
                       step_id: str = "tool") -> dict[str, Any]:
        try:
            args = SearchMemoryInput.model_validate(raw_input)
        except Exception as exc:
            return ToolError(error_code="INVALID_INPUT", message=str(exc), retryable=False).to_dict()

        if self._memory is None:
            return ToolError(error_code="MEMORY_UNAVAILABLE",
                             message="No memory store configured for this session",
                             retryable=False).to_dict()

        import time as _time
        t0 = _time.monotonic()
        try:
            hits = self._memory.search(args.query, user_id=args.user_id, limit=args.limit)
            duration_ms = round((_time.monotonic() - t0) * 1000, 1)
            if self._logger:
                self._logger._write({
                    "event": "memory_op",
                    "step_id": step_id,
                    "operation": "search",
                    "provider": self._memory.provider_name,
                    "user_id": args.user_id,
                    "query_or_content": args.query[:200],
                    "result_count": len(hits),
                    "duration_ms": duration_ms,
                })
            results = [
                MemoryResult(
                    id=h["id"],
                    content=h["content"],
                    score=h.get("score", 0.0),
                    metadata=h.get("metadata", {}),
                )
                for h in hits
            ]
            return SearchMemoryOutput(
                results=results,
                total_found=len(results),
                provider=self._memory.provider_name,
            ).model_dump()
        except Exception as exc:
            logger.error("search_memory failed: %s", exc)
            return ToolError(error_code="MEMORY_ERROR", message=str(exc), retryable=True).to_dict()

    def _add_memory(self, raw_input: dict[str, Any],
                    step_id: str = "tool") -> dict[str, Any]:
        try:
            args = AddMemoryInput.model_validate(raw_input)
        except Exception as exc:
            return ToolError(error_code="INVALID_INPUT", message=str(exc), retryable=False).to_dict()

        if self._memory is None:
            return ToolError(error_code="MEMORY_UNAVAILABLE",
                             message="No memory store configured for this session",
                             retryable=False).to_dict()

        import time as _time
        t0 = _time.monotonic()
        try:
            stored_id = self._memory.add(args.content, user_id=args.user_id,
                                         metadata=args.metadata)
            duration_ms = round((_time.monotonic() - t0) * 1000, 1)
            if self._logger:
                self._logger._write({
                    "event": "memory_op",
                    "step_id": step_id,
                    "operation": "add",
                    "provider": self._memory.provider_name,
                    "user_id": args.user_id,
                    "query_or_content": args.content[:200],
                    "stored_key": stored_id,
                    "duration_ms": duration_ms,
                })
            return AddMemoryOutput(
                stored_id=stored_id,
                provider=self._memory.provider_name,
            ).model_dump()
        except Exception as exc:
            logger.error("add_memory failed: %s", exc)
            return ToolError(error_code="MEMORY_ERROR", message=str(exc), retryable=True).to_dict()

    def _delete_memory(self, raw_input: dict[str, Any],
                       step_id: str = "tool") -> dict[str, Any]:
        try:
            args = DeleteMemoryInput.model_validate(raw_input)
        except Exception as exc:
            return ToolError(error_code="INVALID_INPUT", message=str(exc), retryable=False).to_dict()

        if self._memory is None:
            return ToolError(error_code="MEMORY_UNAVAILABLE",
                             message="No memory store configured for this session",
                             retryable=False).to_dict()

        import time as _time
        t0 = _time.monotonic()
        try:
            deleted = self._memory.delete(args.memory_id, user_id=args.user_id)
            duration_ms = round((_time.monotonic() - t0) * 1000, 1)
            if self._logger:
                self._logger._write({
                    "event": "memory_op",
                    "step_id": step_id,
                    "operation": "delete",
                    "provider": self._memory.provider_name,
                    "user_id": args.user_id,
                    "query_or_content": args.memory_id,
                    "stored_key": args.memory_id,
                    "duration_ms": duration_ms,
                })
            return DeleteMemoryOutput(
                deleted=deleted,
                memory_id=args.memory_id,
                provider=self._memory.provider_name,
            ).model_dump()
        except Exception as exc:
            logger.error("delete_memory failed: %s", exc)
            return ToolError(error_code="MEMORY_ERROR", message=str(exc), retryable=True).to_dict()

    def _search_knowledge_base(self, raw_input: dict[str, Any],
                               step_id: str = "tool") -> dict[str, Any]:
        try:
            args = SearchKBInput.model_validate(raw_input)
        except Exception as exc:
            return ToolError(error_code="INVALID_INPUT", message=str(exc), retryable=False).to_dict()

        # Credential fetched here — after the model has decided to call this tool.
        # The token never entered the model's context.
        token = self._store.get(self._secret_key)

        url = f"{self._catalog_base_url}/api/meta/search/indexsearch"
        request_body = {
            "dsl": {
                "query": {"multi_match": {"query": args.query, "fields": ["name", "displayName", "description"]}},
                "size": args.max_results,
            },
            "attributes": ["name", "description", "typeName", "qualifiedName"],
        }

        import time as _time
        t0 = _time.monotonic()
        try:
            response = httpx.post(
                url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=request_body,
                timeout=10.0,
            )
            duration_ms = round((_time.monotonic() - t0) * 1000, 1)
            response.raise_for_status()
            data = response.json()
            entities = data.get("entities", [])
            assets = [
                CatalogAsset(
                    name=e.get("attributes", {}).get("name", ""),
                    type=e.get("typeName", ""),
                    description=e.get("attributes", {}).get("description", "") or "",
                    qualified_name=e.get("attributes", {}).get("qualifiedName", ""),
                )
                for e in entities
            ]
            result = SearchKBOutput(results=assets, total=len(assets)).model_dump()
            self._log_http(
                step_id=f"{step_id}.http",
                url=url,
                request_body=request_body,
                status_code=response.status_code,
                response_body={"entity_count": len(entities), "approximate_count": data.get("approximateCount")},
                duration_ms=duration_ms,
            )
            return result
        except httpx.HTTPStatusError as e:
            duration_ms = round((_time.monotonic() - t0) * 1000, 1)
            self._log_http(
                step_id=f"{step_id}.http",
                url=url,
                request_body=request_body,
                status_code=e.response.status_code,
                response_body=redact(e.response.text[:500]),
                duration_ms=duration_ms,
                error=f"HTTP {e.response.status_code}",
            )
            return ToolError(error_code="HTTP_ERROR",
                             message=f"API error: {e.response.status_code}",
                             retryable=e.response.status_code >= 500).to_dict()
        except httpx.RequestError as e:
            duration_ms = round((_time.monotonic() - t0) * 1000, 1)
            self._log_http(
                step_id=f"{step_id}.http",
                url=url,
                request_body=request_body,
                status_code=None,
                response_body=None,
                duration_ms=duration_ms,
                error=redact(str(e)),
            )
            return ToolError(error_code="REQUEST_ERROR",
                             message="API unreachable",
                             retryable=True).to_dict()

