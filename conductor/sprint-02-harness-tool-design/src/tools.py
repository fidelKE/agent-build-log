"""
Tool layer for Conductor Sprint 2.

Every tool has:
- Pydantic input schema (validated before execution)
- Pydantic output schema (predictable structure always)
- Typed ToolError with retryable flag (agent decides whether to retry)
- schema_version on every schema definition (RULE-T04)

Write tools carry an idempotency_key so that retries are safe (RULE-T03):
the tool author knows whether re-executing is safe; the model should not guess.

Conductor mode: Q&A + Setup.
"""

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Schema version for dispatch-time mismatch detection (RULE-T04)
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Error contract
# ---------------------------------------------------------------------------

class ToolError(BaseModel):
    error_code: str
    message: str
    retryable: bool
    retry_after: int | None = None  # seconds to wait before retrying; None = no guidance

    def to_dict(self) -> dict:
        result = {"error": True, "error_code": self.error_code, "message": self.message, "retryable": self.retryable}
        if self.retry_after is not None:
            result["retry_after"] = self.retry_after
        return result


# ---------------------------------------------------------------------------
# notes_search — read tool, naturally idempotent
# ---------------------------------------------------------------------------

class NotesSearchInput(BaseModel):
    query: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Search query to find relevant notes",
    )
    max_results: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum number of notes to return",
    )

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


# In-memory notes corpus — replaced by real retrieval in Sprint 6 (RAG).
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
    """Naive keyword overlap score — replaced by vector search in Sprint 6."""
    query_terms = set(query.lower().split())
    text = (note["title"] + " " + note["body"]).lower()
    matches = sum(1 for term in query_terms if term in text)
    return matches / max(len(query_terms), 1)


def notes_search(raw_input: dict) -> dict:
    """
    Search the notes knowledge base.

    Use when: answering 'how do I...', 'what is...', or troubleshooting questions.
    Do NOT use for: action requests (add, create, configure, update, delete, set up),
    creating notes, updating connector config, or executing actions.
    
    Validate input with Pydantic first, then score notes against the query and return the top matches.

    Args:
        raw_input: dict matching NotesSearchInput schema

    Returns:
        NotesSearchOutput dict on success, ToolError dict on failure
    """
    try:
        args = NotesSearchInput.model_validate(raw_input)
    except Exception as exc:
        return ToolError(
            error_code="INVALID_INPUT",
            message=str(exc),
            retryable=False,
        ).to_dict()

    scored = [
        (note, _score(note, args.query))
        for note in _NOTES
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    top = [note for note, score in scored[:args.max_results] if score > 0]

    results = [
        NoteResult(
            id=note["id"],
            title=note["title"],
            snippet=note["body"][:200],
            score=round(_score(note, args.query), 3),
        )
        for note in top
    ]

    return NotesSearchOutput(
        results=results,
        total_found=len(top),
    ).model_dump()


# ---------------------------------------------------------------------------
# create_connector_config — write tool with idempotency key (RULE-T03)
#
# Write tools are not naturally idempotent: calling this twice with the same
# config would create a duplicate. The idempotency_key lets the harness check
# whether this exact operation already ran and return the cached result without
# re-executing — safe for model retries, network retries, and loop re-entries.
# ---------------------------------------------------------------------------

class ConnectorConfigInput(BaseModel):
    idempotency_key: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Unique key for this operation; same key = return cached result, no re-execution",
    )
    connector_type: str = Field(
        ...,
        description="Connector type (e.g. 'snowflake', 'bigquery', 'postgres')",
    )
    display_name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Human-readable name for this connector configuration",
    )

    @field_validator("connector_type")
    @classmethod
    def connector_type_lowercase(cls, v: str) -> str:
        return v.strip().lower()


class ConnectorConfigOutput(BaseModel):
    config_id: str
    connector_type: str
    display_name: str
    created: bool  # True = newly created; False = returned from idempotency cache


# Module-level idempotency cache — persisted to durable storage in Sprint 4.
_IDEMPOTENCY_CACHE: dict[str, dict] = {}


def generate_idempotency_key(task_id: str, tool_name: str, params: dict) -> str:
    """
    Derive a deterministic idempotency key from the operation's identity.

    The key is stable across retries: same task + tool + params → same key.
    Callers should NOT generate random keys — that defeats idempotency.

    Args:
        task_id:   Stable identifier for the parent task (e.g. run_id, session_id).
        tool_name: Name of the tool being called.
        params:    Dict of the fields that make this call unique (exclude idempotency_key itself).

    Returns:
        A deterministic string key (32 hex chars).
    """
    import hashlib
    import json
    payload = json.dumps({"task_id": task_id, "tool": tool_name, "params": params}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def create_connector_config(raw_input: dict) -> dict:
    """
    Create a connector configuration entry.

    Use when: the user has confirmed all required connector fields and wants
    to save the configuration.
    Do NOT use for: reading existing configs, troubleshooting, or Q&A.

    Write tool — carries an idempotency_key. Identical key returns the cached
    result without re-executing; different key creates a new entry.

    Args:
        raw_input: dict matching ConnectorConfigInput schema

    Returns:
        ConnectorConfigOutput dict on success, ToolError dict on failure
    """
    try:
        args = ConnectorConfigInput.model_validate(raw_input)
    except Exception as exc:
        return ToolError(
            error_code="INVALID_INPUT",
            message=str(exc),
            retryable=False,
        ).to_dict()

    if args.idempotency_key in _IDEMPOTENCY_CACHE:
        cached = _IDEMPOTENCY_CACHE[args.idempotency_key]
        print(f"[idempotent hit] key={args.idempotency_key} returning cached config_id={cached['config_id']}")
        # Return the cached result with created=False to signal this was a replay,
        # not a fresh execution.
        return ConnectorConfigOutput(**{**cached, "created": False}).model_dump()

    import uuid
    config_id = f"cfg-{uuid.uuid4().hex[:8]}"
    result = ConnectorConfigOutput(
        config_id=config_id,
        connector_type=args.connector_type,
        display_name=args.display_name,
        created=True,
    )
    _IDEMPOTENCY_CACHE[args.idempotency_key] = result.model_dump()
    return result.model_dump()


# ---------------------------------------------------------------------------
# Tool registry — maps name → callable, exposes schema to the agent loop
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, dict] = {
    "notes_search": {
        "fn": notes_search,
        "schema": {
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
                    "query": {
                        "type": "string",
                        "description": "Search query (1-500 chars)",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max notes to return (1-10, default 3)",
                        "default": 3,
                    },
                },
                "required": ["query"],
            },
        },
    },
    "create_connector_config": {
        "fn": create_connector_config,
        "schema": {
            "name": "create_connector_config",
            "version": SCHEMA_VERSION,
            "description": (
                "Create a connector configuration entry. "
                "Use when the user has confirmed all required fields and wants to save a new connector. "
                "Write tool — requires an idempotency_key to make retries safe. "
                "Do NOT use for reading, troubleshooting, or Q&A."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "idempotency_key": {
                        "type": "string",
                        "description": "Unique key for this operation (max 128 chars) — same key returns cached result",
                    },
                    "connector_type": {
                        "type": "string",
                        "description": "Connector type (e.g. 'snowflake', 'bigquery', 'postgres')",
                    },
                    "display_name": {
                        "type": "string",
                        "description": "Human-readable name for this connector configuration",
                    },
                },
                "required": ["idempotency_key", "connector_type", "display_name"],
            },
        },
    },
}
