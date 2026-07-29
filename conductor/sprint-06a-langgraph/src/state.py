"""
State management for Conductor -- Sprint 6a.

Unchanged from Sprint 5a:
  RunState, SessionStore, CheckpointStore -- same Redis/SQLite storage contracts.

Sprint 6 addition:
  SetupStateMachine -- enforces the Setup mode step sequence in code (RULE-STM01).
  States: IDLE -> READ -> VALIDATE -> WRITE.

Sprint 6a change:
  Tool names updated to bare form (no mcp__conductor__ prefix). Sprint 6 used the
  MCP server prefix; Sprint 6a tools are bound directly via LangGraph ToolNode
  with names matching ToolExecutor.execute() dispatch keys.
  Checked in pre_tool_check node (not hook).
"""

import json
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("CONDUCTOR_DB_PATH", "conductor_state.db")
SESSION_TTL_SECONDS = 3600  # 1 hour


# ---------------------------------------------------------------------------
# Run state -- in-memory only, scoped to a single process invocation
# ---------------------------------------------------------------------------

class RunStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class StepRecord:
    step_id: str
    tool_name: str
    status: str
    duration_ms: float


@dataclass
class RunState:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    step_count: int = 0
    status: RunStatus = RunStatus.IN_PROGRESS
    final_answer: Optional[str] = None
    steps: list[StepRecord] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Setup state machine -- enforces read -> validate -> write (RULE-STM01)
# ---------------------------------------------------------------------------

class SetupState(str, Enum):
    IDLE = "idle"
    READ = "read"
    VALIDATE = "validate"
    WRITE = "write"


# Tools that advance state (requires from_state to match current state)
# Sprint 6a: bare names -- no mcp__conductor__ prefix (LangGraph ToolNode, not MCP server)
_TRANSITION_TOOLS: dict[str, tuple[SetupState, SetupState]] = {
    "read_connector_config": (SetupState.IDLE, SetupState.READ),
    "validate_credentials": (SetupState.READ, SetupState.VALIDATE),
    "write_connector_config": (SetupState.VALIDATE, SetupState.WRITE),
}

# Tools that require a specific state; anything not listed is always allowed
_STATE_GATED_TOOLS: dict[str, set[SetupState]] = {
    "validate_credentials": {SetupState.READ},
    "write_connector_config": {SetupState.VALIDATE},
}


@dataclass
class SetupStateMachine:
    """
    Enforces the Setup mode step sequence read -> validate -> write (RULE-STM01).

    Checked by the PreToolUse hook before every tool call. Write-step tools are
    unreachable until validate has completed -- even under prompt injection asking
    to skip the sequence.
    """
    state: SetupState = SetupState.IDLE

    def is_allowed(self, tool_name: str) -> bool:
        """Return True if this tool may be called in the current state."""
        gated_in = _STATE_GATED_TOOLS.get(tool_name)
        if gated_in is None:
            return True  # not a gated tool, always allowed
        return self.state in gated_in

    def advance(self, tool_name: str) -> bool:
        """
        Advance state if this tool is a valid transition from the current state.
        Returns True if state advanced, False otherwise.
        """
        transition = _TRANSITION_TOOLS.get(tool_name)
        if transition is None:
            return False
        from_state, to_state = transition
        if self.state == from_state:
            self.state = to_state
            return True
        return False


# ---------------------------------------------------------------------------
# Redis session store (Layer 1)
# ---------------------------------------------------------------------------

class SessionStore:
    """
    Active message history for a running session in Redis (Layer 1).

    Key: session:{session_id}:{task_id} -- TTL: SESSION_TTL_SECONDS.
    Falls back to in-memory dict when Redis is unavailable (CI, no Podman).
    Fast path only -- durable message history lives in SQLite via CheckpointStore.
    """

    def __init__(self, redis_url: str | None = None, ttl: int = SESSION_TTL_SECONDS):
        self._ttl = ttl
        self._fallback: dict[str, list[dict]] = {}
        self._redis = None
        url = redis_url or os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        try:
            import redis as redis_lib
            client = redis_lib.Redis.from_url(url, socket_connect_timeout=2, decode_responses=True)
            client.ping()
            self._redis = client
            logger.info("SessionStore: using Redis at %s", url)
        except Exception:
            logger.warning("Redis not reachable -- SessionStore falling back to in-memory")

    def available(self) -> bool:
        return self._redis is not None

    @staticmethod
    def _key(session_id: str, task_id: str) -> str:
        return f"session:{session_id}:{task_id}"

    def save(self, session_id: str, task_id: str, messages: list[dict]) -> None:
        key = self._key(session_id, task_id)
        if self._redis is not None:
            self._redis.set(key, json.dumps(messages), ex=self._ttl)
        else:
            self._fallback[key] = messages

    def load(self, session_id: str, task_id: str) -> list[dict] | None:
        key = self._key(session_id, task_id)
        if self._redis is not None:
            raw = self._redis.get(key)
            return json.loads(raw) if raw is not None else None
        return self._fallback.get(key)

    def delete(self, session_id: str, task_id: str) -> None:
        key = self._key(session_id, task_id)
        if self._redis is not None:
            self._redis.delete(key)
        else:
            self._fallback.pop(key, None)


# ---------------------------------------------------------------------------
# SQLite checkpoint store (Layer 3)
# ---------------------------------------------------------------------------

@dataclass
class AgentState:
    """Durable step progress persisted to SQLite (Layer 3)."""
    session_id: str
    task_id: str
    current_step: int = 0
    total_steps: int = 0
    completed_steps: list[int] = field(default_factory=list)
    status: str = "in_progress"


class CheckpointStore:
    """
    Persists AgentState and message history to SQLite (Layer 3).
    Survives process death and Redis TTL expiry (RULE-STO04).
    """

    def __init__(self, db_path: str = DB_PATH):
        self._db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS checkpoints (
                    session_id TEXT NOT NULL,
                    task_id    TEXT NOT NULL,
                    step       INTEGER NOT NULL DEFAULT 0,
                    payload    TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (session_id, task_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    session_id TEXT NOT NULL,
                    task_id    TEXT NOT NULL,
                    payload    TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (session_id, task_id)
                )
            """)

    def save(self, state: AgentState) -> None:
        payload = json.dumps(asdict(state))
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO checkpoints (session_id, task_id, step, payload)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (session_id, task_id)
                DO UPDATE SET step = excluded.step,
                              payload = excluded.payload,
                              updated_at = datetime('now')
            """, (state.session_id, state.task_id, state.current_step, payload))

    def load(self, session_id: str, task_id: str) -> Optional[AgentState]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM checkpoints WHERE session_id = ? AND task_id = ?",
                (session_id, task_id),
            ).fetchone()
        if row is None:
            return None
        return AgentState(**json.loads(row["payload"]))

    def save_messages(self, session_id: str, task_id: str, messages: list[dict]) -> None:
        payload = json.dumps(messages)
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO messages (session_id, task_id, payload)
                VALUES (?, ?, ?)
                ON CONFLICT (session_id, task_id)
                DO UPDATE SET payload = excluded.payload,
                              updated_at = datetime('now')
            """, (session_id, task_id, payload))

    def load_messages(self, session_id: str, task_id: str) -> list[dict] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM messages WHERE session_id = ? AND task_id = ?",
                (session_id, task_id),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload"])

    def reset(self, session_id: str, task_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM checkpoints WHERE session_id = ? AND task_id = ?",
                (session_id, task_id),
            )
            conn.execute(
                "DELETE FROM messages WHERE session_id = ? AND task_id = ?",
                (session_id, task_id),
            )
