"""Pydantic models for structured output and data contracts.

All LLM outputs are validated against these schemas. This is the
foundation of the "structured output with retry" pattern from the
enterprise agent guide.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────────────────────
# Execution results
# ─────────────────────────────────────────────────────────────────────────────

class ExecutionResult(BaseModel):
    """Result of executing Python code in a sandboxed kernel."""

    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None
    images: List[Dict[str, str]] = Field(default_factory=list)
    success: bool = True
    execution_time_ms: float = 0.0

    @property
    def output(self) -> str:
        parts: list[str] = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(self.stderr)
        if self.error:
            parts.append(f"Error: {self.error}")
        return "\n".join(parts) if parts else "(No output)"

    @property
    def truncated_output(self) -> str:
        out = self.output
        if len(out) > 4000:
            return out[:4000] + f"\n... (truncated, {len(out)} chars total)"
        return out


# ─────────────────────────────────────────────────────────────────────────────
# Plan state
# ─────────────────────────────────────────────────────────────────────────────

class PlanStep(BaseModel):
    number: int
    description: str
    completed: bool = False

    def mark_complete(self) -> None:
        self.completed = True

    def __str__(self) -> str:
        marker = "[x]" if self.completed else "[ ]"
        return f"{self.number}. {marker} {self.description}"


class PlanState(BaseModel):
    steps: List[PlanStep] = Field(default_factory=list)
    raw_text: str = ""

    @property
    def total_steps(self) -> int:
        return len(self.steps)

    @property
    def completed_steps(self) -> int:
        return sum(1 for s in self.steps if s.completed)

    @property
    def pending_steps(self) -> int:
        return self.total_steps - self.completed_steps

    @property
    def is_complete(self) -> bool:
        return self.total_steps > 0 and all(s.completed for s in self.steps)

    @property
    def progress(self) -> str:
        return f"{self.completed_steps}/{self.total_steps}"

    @property
    def current_step(self) -> Optional[PlanStep]:
        for step in self.steps:
            if not step.completed:
                return step
        return None

    def to_markdown(self) -> str:
        return "\n".join(str(s) for s in self.steps)


# ─────────────────────────────────────────────────────────────────────────────
# Event streaming (event sourcing)
# ─────────────────────────────────────────────────────────────────────────────

class EventType(str, Enum):
    # Lifecycle
    AGENT_STARTED = "agent_started"
    AGENT_FINISHED = "agent_finished"
    AGENT_ERROR = "agent_error"
    AGENT_CANCELLED = "agent_cancelled"

    # Round
    ROUND_STARTED = "round_started"
    ROUND_FINISHED = "round_finished"

    # LLM
    LLM_CALL_STARTED = "llm_call_started"
    LLM_CALL_FINISHED = "llm_call_finished"
    LLM_FALLBACK = "llm_fallback"
    LLM_TOKEN_STREAM = "llm_token_stream"

    # Plan
    PLAN_CREATED = "plan_created"
    PLAN_UPDATED = "plan_updated"
    PLAN_STEP_COMPLETED = "plan_step_completed"

    # Execution
    CODE_EXECUTING = "code_executing"
    CODE_SUCCESS = "code_success"
    CODE_FAILED = "code_failed"

    # Output validation
    OUTPUT_VALIDATION_FAILED = "output_validation_failed"
    OUTPUT_VALIDATION_RETRY = "output_validation_retry"

    # Thinking
    THINKING = "thinking"

    # Context management
    CONTEXT_PRUNED = "context_pruned"

    # Sub-agent delegation
    SUBAGENT_STARTED = "subagent_started"
    SUBAGENT_FINISHED = "subagent_finished"

    # Answer
    ANSWER_STARTED = "answer_started"
    ANSWER_DELTA = "answer_delta"
    ANSWER_FINISHED = "answer_finished"


class AgentEvent(BaseModel):
    """An event emitted during an agent run.

    Events are the unit of streaming AND persistence. Every run is
    recorded as an ordered sequence of events, enabling replay,
    Last-Event-ID resume, and audit trails.
    """

    schema_version: str = "1"
    seq: int = 0
    type: EventType
    timestamp: datetime = Field(default_factory=datetime.now)
    run_id: str = ""
    session_id: str = ""
    round_num: int = 0
    message: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)

    # Optional structured payloads
    plan: Optional[PlanState] = None
    code: Optional[str] = None
    result: Optional[ExecutionResult] = None
    error: Optional[str] = None
    delta: Optional[str] = None

    def to_sse(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        lines = [
            f"id: {self.seq}",
            f"event: {self.type.value}",
            f"data: {json.dumps(payload)}",
        ]
        return "\n".join(lines) + "\n\n"


# ─────────────────────────────────────────────────────────────────────────────
# Structured LLM output (validated with retry)
# ─────────────────────────────────────────────────────────────────────────────

class AgentAction(BaseModel):
    """The structured action the agent decides to take in a round.

    The LLM is asked to return JSON matching this schema. Invalid
    output triggers a retry with a correction hint.
    """

    thinking: str = Field(
        default="",
        description="Brief reasoning about the current state and next step.",
    )
    plan_update: Optional[str] = Field(
        default=None,
        description="Updated plan text, if the plan needs revision.",
    )
    code: Optional[str] = Field(
        default=None,
        description="Python code to execute in the sandbox. Omit if not needed.",
    )
    delegate: Optional[dict] = Field(
        default=None,
        description=(
            "Delegate a subtask to a specialist agent. "
            'Format: {"role": "schema_explorer|data_engineer|analyst|visualizer", '
            '"task": "..."}'
        ),
    )
    final_answer: Optional[str] = Field(
        default=None,
        description="The final answer to the user. Set only when the task is complete.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Session state
# ─────────────────────────────────────────────────────────────────────────────

class SessionState(BaseModel):
    session_id: str
    run_id: Optional[str] = None
    model: str
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    plan: Optional[PlanState] = None
    execution_records: List[Dict[str, Any]] = Field(default_factory=list)
    artifacts: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    def update_timestamp(self) -> None:
        self.updated_at = datetime.now()


# ─────────────────────────────────────────────────────────────────────────────
# API request/response models
# ─────────────────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    task: str = Field(..., min_length=1, max_length=10000)
    model: Optional[str] = None
    session_id: Optional[str] = None


class RunResponse(BaseModel):
    run_id: str
    session_id: str
    status: str = "started"


class SessionInfo(BaseModel):
    session_id: str
    created_at: datetime
    updated_at: datetime
    message_count: int
    last_task: Optional[str] = None