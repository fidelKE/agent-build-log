"""
State management for Conductor — Sprint 3.

Three complementary layers, each with a distinct failure contract:

  RunState        In-memory run state for this loop (steps, status, answer).
                  Scoped to a single process invocation. Always starts fresh.

  SessionStore    Redis (Layer 1) — active message history for the current session.
                  Key: session:{session_id}:{task_id}, TTL: 1 hour.
                  Ephemeral by design: acceptable to lose on Redis restart or TTL expiry.
                  Falls back to an in-memory dict when Redis is unavailable (CI, no Podman).

  CheckpointStore SQLite (Layer 3) — durable store that survives process death and TTL expiry.
                  Holds BOTH step progress (current_step, completed_steps, status)
                  AND message history (save_messages / load_messages).
                  Looked up by (session_id, task_id) structured key, not semantic similarity.

Layer assignment rule (RULE-STO03, RULE-STO04):
  Message history (fast path)  → SessionStore (Redis, Layer 1) — TTL cache
  Message history (fallback)   → CheckpointStore (SQLite, Layer 3) — survives TTL expiry
  Step progress                → CheckpointStore (SQLite, Layer 3) only

  Load order: Redis first → SQLite fallback if Redis returns nothing.
  Save order: SQLite first (durable) → Redis (cache). Both written on every step.

SQLite schema is intentionally simple — Postgres is a drop-in via DATABASE_URL.
"""

import json
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    LIMIT_REACHED = "limit_reached"
    ERROR = "error"


@dataclass
class StepRecord:
    step: int
    tool_name: str | None
    tool_input: dict | None
    tool_output: Any
    duration_ms: float
    status: str  # "success" | "error" | "no_tool"


@dataclass
class RunState:
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: RunStatus = RunStatus.RUNNING
    steps: list[StepRecord] = field(default_factory=list)
    final_answer: str | None = None

    def record_step(self, record: StepRecord) -> None:
        self.steps.append(record)

    @property
    def step_count(self) -> int:
        return len(self.steps)


DEFAULT_DB_PATH = Path(__file__).parent.parent / "conductor_state.db"


@dataclass
class AgentState:
    """
    Durable step progress persisted to SQLite (Layer 3).

    Tracks where the agent is in a multi-step flow so it can resume after a crash.
    Does NOT store message history directly — messages are in CheckpointStore.messages
    (durable) and SessionStore (Redis cache).
    """
    session_id: str
    task_id: str
    current_step: int = 0
    total_steps: int = 0
    completed_steps: list[int] = field(default_factory=list)
    status: str = "in_progress"  # in_progress | completed | failed


class CheckpointStore:
    """
    Persists AgentState to SQLite after each step.

    All state is JSON-serialized into a single 'payload' column so the schema
    never needs a migration when AgentState fields are added.
    Credentials never touch this DB — they live in secrets.py only.
    """

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
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
                    step       INTEGER NOT NULL,
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
        """Upsert checkpoint after completing a step."""
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
        """Return the last saved state, or None if no checkpoint exists."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM checkpoints WHERE session_id = ? AND task_id = ?",
                (session_id, task_id),
            ).fetchone()
        if row is None:
            return None
        data = json.loads(row["payload"])
        return AgentState(**data)

    def save_messages(self, session_id: str, task_id: str, messages: list[dict]) -> None:
        """Persist message history to SQLite — durable fallback when Redis key expires."""
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
        """Return persisted message history, or None if none exists."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM messages WHERE session_id = ? AND task_id = ?",
                (session_id, task_id),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload"])

    def reset(self, session_id: str, task_id: str) -> None:
        """Clear checkpoint and message history — next run starts from step 1."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM checkpoints WHERE session_id = ? AND task_id = ?",
                (session_id, task_id),
            )
            conn.execute(
                "DELETE FROM messages WHERE session_id = ? AND task_id = ?",
                (session_id, task_id),
            )

    def dump_all(self) -> list[dict]:
        """Return all checkpoint rows as dicts. Used in tests to verify no credentials leaked."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM checkpoints").fetchall()
        return [dict(row) for row in rows]

    def dump_messages(self) -> list[dict]:
        """Return all messages rows as dicts. Used in tests to verify no credentials leaked."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM messages").fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Redis session store (Layer 1)
# ---------------------------------------------------------------------------

SESSION_TTL_SECONDS = 3600  # 1 hour — messages expire with the session


class SessionStore:
    """
    Stores the active message history for a running session in Redis (Layer 1).

    Key: session:{session_id}:{task_id}
    Value: JSON-serialised list[dict] — the messages array passed to the LLM.
    TTL: SESSION_TTL_SECONDS (refreshed on every write).

    Falls back to an in-memory dict when Redis is unavailable (CI, no Podman).
    The fallback has no TTL and no cross-process visibility — it is local to the
    current process lifetime only. This matches Layer 1 semantics: ephemeral,
    fast, acceptable to lose.

    Redis is the fast path only. Durable message history lives in SQLite (Layer 3)
    via CheckpointStore.save_messages(). If Redis returns nothing on load, the agent
    falls back to SQLite — TTL expiry does not kill resurrection.
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
            logger.warning("Redis not reachable - SessionStore falling back to in-memory")

    def available(self) -> bool:
        return self._redis is not None

    @staticmethod
    def _key(session_id: str, task_id: str) -> str:
        return f"session:{session_id}:{task_id}"

    def save(self, session_id: str, task_id: str, messages: list[dict]) -> None:
        """Persist the message list; refresh TTL on every write."""
        key = self._key(session_id, task_id)
        serialised = json.dumps(messages)
        if self._redis is not None:
            self._redis.set(key, serialised, ex=self._ttl)
        else:
            self._fallback[key] = messages

    def load(self, session_id: str, task_id: str) -> list[dict] | None:
        """Return saved messages, or None if the key does not exist / has expired."""
        key = self._key(session_id, task_id)
        if self._redis is not None:
            raw = self._redis.get(key)
            return json.loads(raw) if raw is not None else None
        return self._fallback.get(key)

    def delete(self, session_id: str, task_id: str) -> None:
        """Remove the session key (called on explicit restart)."""
        key = self._key(session_id, task_id)
        if self._redis is not None:
            self._redis.delete(key)
        else:
            self._fallback.pop(key, None)
