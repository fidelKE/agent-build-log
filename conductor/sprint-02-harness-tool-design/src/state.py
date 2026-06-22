"""
In-memory run state for one agent session.

Scoped to a single run_id. Tracks step count, tool results, and status.
Does not persist across sessions — that's Sprint 3 (SQLite checkpointing).
"""

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


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
