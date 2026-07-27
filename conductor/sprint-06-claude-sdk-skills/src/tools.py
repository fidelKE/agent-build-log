"""
Tool definitions for Conductor -- Sprint 6.

Sprint 5a and earlier: ToolExecutor dispatched tools in the custom while-True loop.
Sprint 6: tools are exposed as in-process MCP functions via build_mcp_server().
ToolExecutor is retained for direct testing (tests call it without SDK overhead).

All tools follow the same three rules (RULE-T01/T02/T03):
  - Pydantic model validates every input before any logic runs
  - Output returned as .model_dump() (typed, not raw dict)
  - ToolError used for all error cases (consistent shape for the model)

New Sprint 6 tools (Setup mode + Troubleshooting):
  check_connector_status  -- in-process status check (no subprocess)
  read_connector_config   -- first step of Setup state machine
  validate_credentials    -- second step (requires READ state)
  write_connector_config  -- third step (requires VALIDATE state, HITL-gated)
"""

import json
import logging
import time as _time
from typing import Any

import httpx
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sprint 1 -- Error contract + notes_search (in-memory KB)
# ---------------------------------------------------------------------------

class ToolError(BaseModel):
    error_code: str
    message: str
    retryable: bool

    def to_dict(self) -> dict[str, Any]:
        return {"error": True, **self.model_dump()}


_NOTES: dict[str, str] = {
    "note-001": "Snowflake connector requires ACCOUNTADMIN or SYSADMIN role for metadata access.",
    "note-002": "BigQuery connector uses a service account JSON key -- ensure roles/bigquery.dataViewer is granted.",
    "note-003": "Redshift connector requires a superuser or a user with SELECT on information_schema.",
    "note-004": "dbt Core integration: run 'dbt docs generate' before the first sync to build the manifest.",
    "note-005": "OAuth tokens expire after 1 hour by default -- enable token refresh in connector settings.",
    "note-006": "Connection timeouts on Snowflake often indicate the warehouse is suspended -- check auto-resume setting.",
    "note-007": "Missing tables in catalog: verify the schema filter includes the target schema names.",
    "note-008": "Postgres connector: ensure pg_hba.conf allows the connector's IP range for the target database.",
}


class NotesSearchInput(BaseModel):
    query: str = Field(..., min_length=1, max_length=500,
                       description="Search query to find relevant integration notes")
    max_results: int = Field(default=3, ge=1, le=10,
                             description="Maximum number of results to return")

    @field_validator("query")
    @classmethod
    def query_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query must not be blank")
        return v.strip()


class NoteResult(BaseModel):
    id: str
    content: str
    relevance: str


class NotesSearchOutput(BaseModel):
    results: list[NoteResult]
    total_found: int


def notes_search(raw_input: dict[str, Any]) -> dict[str, Any]:
    try:
        args = NotesSearchInput.model_validate(raw_input)
    except Exception as exc:
        return ToolError(error_code="INVALID_INPUT", message=str(exc), retryable=False).to_dict()

    q_lower = args.query.lower()
    matches = [
        NoteResult(id=note_id, content=content, relevance="keyword")
        for note_id, content in _NOTES.items()
        if any(word in content.lower() for word in q_lower.split())
    ][:args.max_results]

    return NotesSearchOutput(results=matches, total_found=len(matches)).model_dump()


# ---------------------------------------------------------------------------
# Sprint 3 -- Pydantic schemas for search_knowledge_base
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


# ---------------------------------------------------------------------------
# Sprint 4 -- Memory tool Pydantic models (RULE-T01/T02/T03, RULE-MEM01)
# ---------------------------------------------------------------------------

class SearchMemoryInput(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    user_id: str = Field(..., min_length=1, max_length=128)
    limit: int = Field(default=5, ge=1, le=20)

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
    content: str = Field(..., min_length=1, max_length=2000)
    user_id: str = Field(..., min_length=1, max_length=128)
    metadata: dict = Field(default_factory=dict)

    @field_validator("metadata", mode="before")
    @classmethod
    def coerce_metadata(cls, v: Any) -> dict:
        if isinstance(v, str):
            return {"note": v}
        return v or {}

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
    memory_id: str = Field(..., min_length=1, max_length=256)
    user_id: str = Field(..., min_length=1, max_length=128)

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


# ---------------------------------------------------------------------------
# Sprint 6 -- New Setup + Troubleshooting tool models
# ---------------------------------------------------------------------------

class ConnectorStatusInput(BaseModel):
    connector_id: str = Field(..., min_length=1, max_length=256,
                               description="Connector identifier to check")

    @field_validator("connector_id")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("connector_id must not be blank")
        return v.strip()


class ConnectorStatusOutput(BaseModel):
    connector_id: str
    status: str  # "live" | "error" | "degraded" | "unknown"
    last_sync: str | None
    error_message: str | None
    check_duration_ms: float


class ReadConnectorConfigInput(BaseModel):
    connector_id: str = Field(..., min_length=1, max_length=256)

    @field_validator("connector_id")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("connector_id must not be blank")
        return v.strip()


class ConnectorConfig(BaseModel):
    connector_id: str
    connector_type: str
    host: str
    port: int
    database: str
    schema_filter: list[str]
    status: str


class ReadConnectorConfigOutput(BaseModel):
    config: ConnectorConfig
    read_at: str


class ValidateCredentialsInput(BaseModel):
    connector_id: str = Field(..., min_length=1, max_length=256)
    credentials: dict = Field(..., description="Credential fields to validate (no secrets stored)")

    @field_validator("connector_id")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("connector_id must not be blank")
        return v.strip()


class ValidateCredentialsOutput(BaseModel):
    connector_id: str
    valid: bool
    errors: list[str]
    validation_duration_ms: float


class WriteConnectorConfigInput(BaseModel):
    connector_id: str = Field(..., min_length=1, max_length=256)
    config_patch: dict = Field(..., description="Fields to update in the connector config")

    @field_validator("connector_id")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("connector_id must not be blank")
        return v.strip()


class WriteConnectorConfigOutput(BaseModel):
    connector_id: str
    written: bool
    fields_updated: list[str]
    write_duration_ms: float


# ---------------------------------------------------------------------------
# ToolExecutor -- used by tests and as the implementation backing MCP tools
# ---------------------------------------------------------------------------

class ToolExecutor:
    """
    Executes tool calls with credential injection for authenticated tools.

    Sprint 6: also backs the MCP tool wrappers in build_mcp_server().
    Tests call execute() directly -- no SDK overhead.
    """

    def __init__(
        self,
        secret_store=None,
        memory_store=None,
        catalog_base_url: str = "",
        structured_logger=None,
        secret_key: str = "catalog_token",
    ):
        self._secrets = secret_store
        self._memory = memory_store
        self._catalog_base_url = catalog_base_url
        self._secret_key = secret_key
        self._logger = structured_logger

    def _log_http(self, step_id: str, url: str, request_body: dict,
                  status_code: int | None, response_body: Any,
                  duration_ms: float, error: str | None = None) -> None:
        if self._logger is None:
            return
        self._logger._write({
            "event": "http_call",
            "step_id": step_id,
            "http.url": url,
            "http.method": "POST",
            "http.request_body": request_body,
            # Authorization header intentionally absent -- credential injection
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
        if tool_name == "check_connector_status":
            return self._check_connector_status(tool_input, step_id=step_id)
        if tool_name == "read_connector_config":
            return self._read_connector_config(tool_input, step_id=step_id)
        if tool_name == "validate_credentials":
            return self._validate_credentials(tool_input, step_id=step_id)
        if tool_name == "write_connector_config":
            return self._write_connector_config(tool_input, step_id=step_id)
        raise ValueError(f"Unknown tool: {tool_name}")

    def _search_knowledge_base(self, raw_input: dict[str, Any],
                                step_id: str = "tool") -> dict[str, Any]:
        try:
            args = SearchKBInput.model_validate(raw_input)
        except Exception as exc:
            return ToolError(error_code="INVALID_INPUT", message=str(exc), retryable=False).to_dict()

        if not self._secrets:
            return ToolError(error_code="NO_SECRET_STORE",
                             message="Secret store not configured", retryable=False).to_dict()

        token = self._secrets.get(self._secret_key)
        if not token:
            return ToolError(error_code="MISSING_CREDENTIAL",
                             message="Catalog token not found in secret store", retryable=False).to_dict()

        url = f"{self._catalog_base_url}/api/search"
        payload = {"query": args.query, "limit": args.max_results}
        t0 = _time.monotonic()
        try:
            resp = httpx.post(url, json=payload,
                              headers={"Authorization": f"Bearer {token}"},
                              timeout=10.0)
            duration_ms = round((_time.monotonic() - t0) * 1000, 1)
            resp.raise_for_status()
            data = resp.json()
            assets = [CatalogAsset(**a) for a in data.get("assets", [])]
            self._log_http(step_id, url, payload, resp.status_code,
                           {"total": len(assets)}, duration_ms)
            return SearchKBOutput(results=assets, total=len(assets)).model_dump()
        except Exception as exc:
            duration_ms = round((_time.monotonic() - t0) * 1000, 1)
            self._log_http(step_id, url, payload, None, None, duration_ms, error=str(exc))
            return ToolError(error_code="KB_ERROR", message=str(exc), retryable=True).to_dict()

    def _search_memory(self, raw_input: dict[str, Any],
                       step_id: str = "tool") -> dict[str, Any]:
        try:
            args = SearchMemoryInput.model_validate(raw_input)
        except Exception as exc:
            return ToolError(error_code="INVALID_INPUT", message=str(exc), retryable=False).to_dict()

        if self._memory is None:
            return ToolError(error_code="MEMORY_UNAVAILABLE",
                             message="No memory store configured", retryable=False).to_dict()

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
                    "results_count": len(hits),
                    "duration_ms": duration_ms,
                })
            results = [
                MemoryResult(
                    id=h["id"], content=h["content"],
                    score=h.get("score", 0.0), metadata=h.get("metadata", {})
                )
                for h in hits
            ]
            return SearchMemoryOutput(
                results=results, total_found=len(results),
                provider=self._memory.provider_name
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
                             message="No memory store configured", retryable=False).to_dict()

        t0 = _time.monotonic()
        try:
            stored_id = self._memory.add(args.content, user_id=args.user_id, metadata=args.metadata)
            duration_ms = round((_time.monotonic() - t0) * 1000, 1)
            if self._logger:
                self._logger._write({
                    "event": "memory_op",
                    "step_id": step_id,
                    "operation": "add",
                    "provider": self._memory.provider_name,
                    "user_id": args.user_id,
                    "query_or_content": args.content[:200],
                    "duration_ms": duration_ms,
                })
            return AddMemoryOutput(stored_id=stored_id,
                                   provider=self._memory.provider_name).model_dump()
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
                             message="No memory store configured", retryable=False).to_dict()

        try:
            deleted = self._memory.delete(args.memory_id, user_id=args.user_id)
            return DeleteMemoryOutput(
                deleted=deleted, memory_id=args.memory_id,
                provider=self._memory.provider_name
            ).model_dump()
        except Exception as exc:
            logger.error("delete_memory failed: %s", exc)
            return ToolError(error_code="MEMORY_ERROR", message=str(exc), retryable=True).to_dict()

    # --- Sprint 6: Setup + Troubleshooting tools ----------------------------

    def _check_connector_status(self, raw_input: dict[str, Any],
                                 step_id: str = "tool") -> dict[str, Any]:
        try:
            args = ConnectorStatusInput.model_validate(raw_input)
        except Exception as exc:
            return ToolError(error_code="INVALID_INPUT", message=str(exc), retryable=False).to_dict()

        t0 = _time.monotonic()
        # ponytail: stub returns a realistic shape; replace with real API call in Lab 7a+
        status_map = {
            "snowflake-prod": ("live", "2026-06-23T08:00:00Z", None),
            "bigquery-analytics": ("degraded", "2026-06-22T20:00:00Z", "Quota exceeded on BQ project"),
            "postgres-warehouse": ("error", None, "Connection refused: port 5432"),
        }
        status, last_sync, error_msg = status_map.get(
            args.connector_id, ("unknown", None, "Connector not found")
        )
        duration_ms = round((_time.monotonic() - t0) * 1000, 1)
        if self._logger:
            self._logger._write({
                "event": "connector_status_check",
                "step_id": step_id,
                "connector_id": args.connector_id,
                "status": status,
                "duration_ms": duration_ms,
            })
        return ConnectorStatusOutput(
            connector_id=args.connector_id,
            status=status,
            last_sync=last_sync,
            error_message=error_msg,
            check_duration_ms=duration_ms,
        ).model_dump()

    def _read_connector_config(self, raw_input: dict[str, Any],
                                step_id: str = "tool") -> dict[str, Any]:
        try:
            args = ReadConnectorConfigInput.model_validate(raw_input)
        except Exception as exc:
            return ToolError(error_code="INVALID_INPUT", message=str(exc), retryable=False).to_dict()

        import time as t_mod
        # ponytail: stub config; Lab 7a+ wires this to the real catalog API
        config = ConnectorConfig(
            connector_id=args.connector_id,
            connector_type="snowflake",
            host="account.snowflakecomputing.com",
            port=443,
            database="PROD_DB",
            schema_filter=["PUBLIC", "ANALYTICS"],
            status="active",
        )
        return ReadConnectorConfigOutput(
            config=config,
            read_at=t_mod.strftime("%Y-%m-%dT%H:%M:%SZ", t_mod.gmtime()),
        ).model_dump()

    def _validate_credentials(self, raw_input: dict[str, Any],
                               step_id: str = "tool") -> dict[str, Any]:
        try:
            args = ValidateCredentialsInput.model_validate(raw_input)
        except Exception as exc:
            return ToolError(error_code="INVALID_INPUT", message=str(exc), retryable=False).to_dict()

        t0 = _time.monotonic()
        errors: list[str] = []
        creds = args.credentials
        if not creds.get("username"):
            errors.append("username is required")
        if not creds.get("password") and not creds.get("private_key"):
            errors.append("password or private_key is required")
        # ponytail: stub validation; real validation calls the connector API
        valid = len(errors) == 0
        duration_ms = round((_time.monotonic() - t0) * 1000, 1)
        if self._logger:
            self._logger._write({
                "event": "credential_validation",
                "step_id": step_id,
                "connector_id": args.connector_id,
                "valid": valid,
                "errors": errors,
                "duration_ms": duration_ms,
            })
        return ValidateCredentialsOutput(
            connector_id=args.connector_id,
            valid=valid,
            errors=errors,
            validation_duration_ms=duration_ms,
        ).model_dump()

    def _write_connector_config(self, raw_input: dict[str, Any],
                                 step_id: str = "tool") -> dict[str, Any]:
        try:
            args = WriteConnectorConfigInput.model_validate(raw_input)
        except Exception as exc:
            return ToolError(error_code="INVALID_INPUT", message=str(exc), retryable=False).to_dict()

        t0 = _time.monotonic()
        fields_updated = list(args.config_patch.keys())
        duration_ms = round((_time.monotonic() - t0) * 1000, 1)
        if self._logger:
            self._logger._write({
                "event": "connector_config_write",
                "step_id": step_id,
                "connector_id": args.connector_id,
                "fields_updated": fields_updated,
                "duration_ms": duration_ms,
            })
        return WriteConnectorConfigOutput(
            connector_id=args.connector_id,
            written=True,
            fields_updated=fields_updated,
            write_duration_ms=duration_ms,
        ).model_dump()


# ---------------------------------------------------------------------------
# Sprint 6 -- In-process MCP server wrapping ToolExecutor
# ---------------------------------------------------------------------------

def build_mcp_server(secret_store, memory_store, catalog_base_url: str, logger_inst):
    """
    Create an in-process MCP server exposing all Conductor tools.

    Every MCP tool calls the corresponding ToolExecutor method, keeping validation
    and business logic in one place (tested without SDK overhead in unit tests).
    Returns a create_sdk_mcp_server object ready for ClaudeAgentOptions.mcp_servers.
    """
    from claude_agent_sdk import tool as sdk_tool, create_sdk_mcp_server  # ponytail: verify pkg name in Phase 4

    executor = ToolExecutor(
        secret_store=secret_store,
        memory_store=memory_store,
        catalog_base_url=catalog_base_url,
        structured_logger=logger_inst,
    )

    def _mcp_result(data: dict) -> dict:
        return {"content": [{"type": "text", "text": json.dumps(data)}]}

    @sdk_tool("notes_search", "Search integration troubleshooting notes",
              {"query": {"type": "string"}, "max_results": {"type": "integer"}})
    async def mcp_notes_search(args: dict) -> dict:
        return _mcp_result(executor.execute("notes_search", args))

    @sdk_tool("search_knowledge_base", "Search the data catalog for assets",
              {"query": {"type": "string"}, "max_results": {"type": "integer"}})
    async def mcp_search_knowledge_base(args: dict) -> dict:
        return _mcp_result(executor.execute("search_knowledge_base", args))

    @sdk_tool("search_memory", "Search user memory for past session context",
              {"query": {"type": "string"}, "user_id": {"type": "string"}, "limit": {"type": "integer"}})
    async def mcp_search_memory(args: dict) -> dict:
        return _mcp_result(executor.execute("search_memory", args))

    @sdk_tool("add_memory", "Store a fact for future sessions",
              {"content": {"type": "string"}, "user_id": {"type": "string"}, "metadata": {"type": "object"}})
    async def mcp_add_memory(args: dict) -> dict:
        return _mcp_result(executor.execute("add_memory", args))

    @sdk_tool("delete_memory", "Delete a stored memory by ID",
              {"memory_id": {"type": "string"}, "user_id": {"type": "string"}})
    async def mcp_delete_memory(args: dict) -> dict:
        return _mcp_result(executor.execute("delete_memory", args))

    @sdk_tool("check_connector_status", "Check live status of a connector",
              {"connector_id": {"type": "string"}})
    async def mcp_check_connector_status(args: dict) -> dict:
        return _mcp_result(executor.execute("check_connector_status", args))

    @sdk_tool("read_connector_config", "Read current connector configuration (Setup: step 1)",
              {"connector_id": {"type": "string"}})
    async def mcp_read_connector_config(args: dict) -> dict:
        return _mcp_result(executor.execute("read_connector_config", args))

    @sdk_tool("validate_credentials", "Validate connector credentials without writing (Setup: step 2)",
              {"connector_id": {"type": "string"}, "credentials": {"type": "object"}})
    async def mcp_validate_credentials(args: dict) -> dict:
        return _mcp_result(executor.execute("validate_credentials", args))

    @sdk_tool("write_connector_config", "Write updated connector configuration (Setup: step 3, HITL-gated)",
              {"connector_id": {"type": "string"}, "config_patch": {"type": "object"}})
    async def mcp_write_connector_config(args: dict) -> dict:
        return _mcp_result(executor.execute("write_connector_config", args))

    return create_sdk_mcp_server(
        name="conductor",
        version="1.0.0",
        tools=[
            mcp_notes_search,
            mcp_search_knowledge_base,
            mcp_search_memory,
            mcp_add_memory,
            mcp_delete_memory,
            mcp_check_connector_status,
            mcp_read_connector_config,
            mcp_validate_credentials,
            mcp_write_connector_config,
        ],
    )


# ---------------------------------------------------------------------------
# Tool schemas exposed to the model via messages API (kept for eval runner)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "notes_search",
        "description": "Search integration troubleshooting notes for relevant guidance.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "default": 3},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_knowledge_base",
        "description": "Search the data catalog for assets matching the query.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 3},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_memory",
        "description": "Search user's past session memory for relevant context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "user_id": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query", "user_id"],
        },
    },
    {
        "name": "add_memory",
        "description": "Store a fact about this user's environment for future sessions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "user_id": {"type": "string"},
                "metadata": {"type": "object"},
            },
            "required": ["content", "user_id"],
        },
    },
    {
        "name": "delete_memory",
        "description": "Delete a stored memory entry by its ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "user_id": {"type": "string"},
            },
            "required": ["memory_id", "user_id"],
        },
    },
]
