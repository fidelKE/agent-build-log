"""
Tool layer for Conductor — Sprint 4.

Sprint 1: notes_search — in-memory knowledge base, Pydantic-validated, no auth.
Sprint 2: create_connector_config — write tool with idempotency key. SCHEMA_VERSION
          constant for atomic version bumps. Action-verb exclusions in notes_search
          schema description.
Sprint 4: search_knowledge_base — real REST API, credential injection via ToolExecutor.
          The model never sees the API token. The harness fetches it from the secret
          store at tool call time and injects it into the outbound request.

All tools remain across sprints (cumulative snapshot). The agent uses notes_search for
fast local lookups, create_connector_config for write operations, and
search_knowledge_base for live queries against the real data catalog.

Conductor modes:
  notes_search            → Q&A (local KB, no network, no credentials needed)
  create_connector_config → Setup (write tool, idempotent via idempotency_key)
  search_knowledge_base   → Q&A + Troubleshooting (live catalog, credential injection)
"""

import logging
import uuid
from typing import Any

import httpx
from pydantic import BaseModel, Field, field_validator

from .secrets import SecretStore, redact

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"


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
# Sprint 2 — create_connector_config (write tool, idempotent via idempotency_key)
# ---------------------------------------------------------------------------

_IDEMPOTENCY_CACHE: dict[str, dict] = {}


def generate_idempotency_key(task_id: str, tool_name: str, params: dict) -> str:
    """
    Derive a deterministic idempotency key from the operation's identity.

    Same task + tool + params always produces the same key — safe across retries.

    Args:
        task_id:   Stable identifier for the parent task (e.g. run_id, session_id).
        tool_name: Name of the tool being called.
        params:    Fields that make this call unique (exclude idempotency_key itself).

    Returns:
        A deterministic string key (32 hex chars).
    """
    import hashlib
    import json
    payload = json.dumps({"task_id": task_id, "tool": tool_name, "params": params}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


class ConnectorConfigInput(BaseModel):
    idempotency_key: str = Field(..., min_length=1, max_length=128,
                                  description="Unique key to prevent duplicate config creation")
    connector_type: str = Field(..., min_length=1, max_length=64,
                                 description="Type of connector (e.g. snowflake, bigquery)")
    display_name: str = Field(..., min_length=1, max_length=128,
                               description="Human-readable name for the connector")


class ConnectorConfigOutput(BaseModel):
    config_id: str
    connector_type: str
    display_name: str
    created: bool


def create_connector_config(raw_input: dict) -> dict:
    """
    Create a connector configuration. Idempotent: same idempotency_key returns the
    cached result with created=False instead of creating a duplicate.
    """
    try:
        args = ConnectorConfigInput.model_validate(raw_input)
    except Exception as exc:
        return ToolError(error_code="INVALID_INPUT", message=str(exc), retryable=False).to_dict()

    if args.idempotency_key in _IDEMPOTENCY_CACHE:
        cached = _IDEMPOTENCY_CACHE[args.idempotency_key]
        print(f"[idempotent hit] key={args.idempotency_key} returning cached config_id={cached['config_id']}")
        return ConnectorConfigOutput(**{**cached, "created": False}).model_dump()

    config_id = "cfg-" + uuid.uuid4().hex[:8]
    record = {"config_id": config_id, "connector_type": args.connector_type,
              "display_name": args.display_name}
    _IDEMPOTENCY_CACHE[args.idempotency_key] = record
    return ConnectorConfigOutput(**record, created=True).model_dump()


# ---------------------------------------------------------------------------
# Sprint 4 — ToolExecutor with credential injection (search_knowledge_base)
# ---------------------------------------------------------------------------

# Tool schemas exposed to the model. No auth fields anywhere — intentional.
TOOL_SCHEMAS = [
    {
        "name": "notes_search",
        "version": SCHEMA_VERSION,
        "description": (
            "Search the integration knowledge base for setup guides, "
            "troubleshooting steps, and how-to notes. "
            "Use when the user asks 'how do I...', 'what is...', or reports an error. "
            "Do NOT use for action requests — if the user says 'add', 'create', 'configure', "
            "'update', 'delete', or 'set up' anything, do not call this tool. "
            "Respond instead: 'I can help you find information and troubleshoot issues, "
            "but I cannot make changes to configurations directly.' "
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
        "version": SCHEMA_VERSION,
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
        "name": "create_connector_config",
        "version": SCHEMA_VERSION,
        "description": (
            "Create a new connector configuration. Use when the user explicitly asks to "
            "set up or create a connector. Idempotent: provide a unique idempotency_key "
            "to prevent duplicate configurations if the request is retried."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "idempotency_key": {"type": "string", "description": "Unique key to prevent duplicate config creation (1-128 chars)"},
                "connector_type": {"type": "string", "description": "Type of connector (e.g. snowflake, bigquery)"},
                "display_name": {"type": "string", "description": "Human-readable name for the connector"},
            },
            "required": ["idempotency_key", "connector_type", "display_name"],
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
    The model never sees the credential.

    Pass structured_logger to enable http_call trace events in verbose mode.
    The Authorization header is always stripped before logging — the token
    never appears in the trace even with full HTTP debug logging enabled.
    This is the proof: credential injection means there is nothing to leak.
    """

    def __init__(
        self,
        secret_store: SecretStore,
        catalog_base_url: str,
        secret_key: str = "catalog-api-token",
        structured_logger: Any = None,
    ):
        self._store = secret_store
        self._catalog_base_url = catalog_base_url.rstrip("/")
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
        if tool_name == "create_connector_config":
            return create_connector_config(tool_input)
        if tool_name == "search_knowledge_base":
            return self._search_knowledge_base(tool_input, step_id=step_id)
        raise ValueError(f"Unknown tool: {tool_name}")

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

