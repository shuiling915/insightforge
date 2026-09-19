"""The agent loop: observation → action → observation → ... → answer.

Key production features:
  - Structured output with validation + retry
  - Code safety check before execution
  - Plan completeness guard (no early answers)
  - Token budget enforcement
  - Event streaming for UI / audit
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Generator, List, Optional

from insightforge.agent.context import ContextManager
from insightforge.agent.prompts import build_system_prompt
from insightforge.agent.subagent import DelegateRequest, SubAgent
from insightforge.config import Settings
from insightforge.execution.base import ExecutorBackend
from insightforge.gateway.llm import LLMError, LLMGateway
from insightforge.schema.models import (
    AgentAction,
    AgentEvent,
    EventType,
    ExecutionResult,
    PlanState,
)
from insightforge.security.safety import CodeSafetyChecker
from insightforge.session.store import SessionStoreBackend

logger = logging.getLogger(__name__)


class DataAnalysisAgent:
    """Autonomous data analysis agent with a code-execution tool."""

    def __init__(
        self,
        settings: Settings,
        executor: ExecutorBackend,
        gateway: Optional[LLMGateway] = None,
        store: Optional[SessionStoreBackend] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        self.settings = settings
        self.executor = executor
        self.gateway = gateway or LLMGateway(settings)
        self.safety = CodeSafetyChecker()
        self.store = store
        self.cancel_event = cancel_event or threading.Event()
        self.context_manager = ContextManager(
            max_context_tokens=getattr(settings, "context_window_tokens", 128000),
            keep_recent_rounds=getattr(settings, "context_keep_recent_rounds", 6),
        )
        self.subagent = SubAgent(settings, executor, gateway, max_rounds=8)

        self.messages: List[dict] = []
        self.plan: Optional[PlanState] = None
        self.round_num = 0
        self.answer: Optional[str] = None
        self.run_id = uuid.uuid4().hex
        self.session_id = ""
        self._token_estimate = 0
        self._cancelled = False

    # ── public API ──────────────────────────────────────────────────────────

    def run(self, task: str) -> str:
        for _ in self.run_stream(task):
            pass
        return self.answer or "No answer generated."

    def run_stream(self, task: str) -> Generator[AgentEvent, None, None]:
        yield self._emit(EventType.AGENT_STARTED, f"Starting: {task}")

        self.messages = [
            {"role": "system", "content": build_system_prompt()},
            {"role": "user", "content": f"Task: {task}"},
        ]
        self.round_num = 0
        self.answer = None

        while self.round_num < self.settings.max_rounds:
            if self.cancel_event.is_set():
                self._cancelled = True
                yield self._emit(EventType.AGENT_CANCELLED, "Run cancelled by user.")
                break

            self.round_num += 1
            yield self._emit(
                EventType.ROUND_STARTED,
                f"Round {self.round_num}/{self.settings.max_rounds}",
            )

            # Context management: prune if approaching token limit
            pruned_msgs, stats = self.context_manager.manage(self.messages)
            if stats.pruned:
                self.messages = pruned_msgs
                yield self._emit(
                    EventType.CONTEXT_PRUNED,
                    f"Context pruned: {stats.total_tokens} tokens "
                    f"({stats.message_count} messages)",
                )

            # 1. Get structured action from LLM
            yield self._emit(EventType.LLM_CALL_STARTED)
            try:
                action = self.gateway.chat_structured(
                    self.messages, response_model=AgentAction
                )
            except LLMError as e:
                yield self._emit(EventType.AGENT_ERROR, f"LLM failed: {e}")
                break
            yield self._emit(EventType.LLM_CALL_FINISHED, action.thinking[:200])

            # Account for token budget (rough estimate)
            self._token_estimate += len(action.model_dump_json()) // 4
            if self._token_estimate > self.settings.max_tokens_per_run:
                yield self._emit(
                    EventType.AGENT_ERROR,
                    f"Token budget exceeded ({self._token_estimate} tokens).",
                )
                break

            # 2. Handle thinking / plan update
            if action.thinking:
                yield self._emit(EventType.THINKING, action.thinking)
            if action.plan_update:
                self.plan = self._parse_plan(action.plan_update)
                yield self._emit(EventType.PLAN_UPDATED, plan=self.plan)

            # 3. Check for final answer
            if action.final_answer:
                if self._should_reject_answer():
                    yield self._emit(
                        EventType.OUTPUT_VALIDATION_FAILED,
                        "Plan not complete; asking agent to continue.",
                    )
                    self.messages.append({"role": "assistant", "content": action.model_dump_json()})
                    self.messages.append({
                        "role": "user",
                        "content": "Your plan still has uncompleted steps. "
                        "Please finish all steps before giving the final answer.",
                    })
                    continue

                self.answer = action.final_answer
                yield self._emit(EventType.ANSWER_FINISHED, self.answer)
                break

            # 3b. Handle sub-agent delegation
            if action.delegate:
                yield from self._handle_delegation(action)
                continue

            # 4. Execute code
            if action.code:
                # Safety check
                report = self.safety.check(action.code)
                if not report.safe:
                    yield self._emit(EventType.CODE_FAILED, report.message)
                    self.messages.append({"role": "assistant", "content": action.model_dump_json()})
                    self.messages.append({
                        "role": "user",
                        "content": f"Code rejected by safety checker:\n{report.message}\n"
                        "Please rewrite without the blocked operations.",
                    })
                    continue

                yield self._emit(EventType.CODE_EXECUTING, "执行代码", code=action.code)
                result = self.executor.execute(action.code)

                if result.success:
                    yield self._emit(
                        EventType.CODE_SUCCESS,
                        result.truncated_output,
                        result=result,
                    )
                    # Mark current plan step complete if we have a plan
                    if self.plan and self.plan.current_step:
                        self.plan.current_step.mark_complete()
                        yield self._emit(EventType.PLAN_STEP_COMPLETED)
                else:
                    yield self._emit(
                        EventType.CODE_FAILED,
                        result.truncated_output,
                        result=result,
                    )

                # Feed execution result back to the model
                self.messages.append({"role": "assistant", "content": action.model_dump_json()})
                self.messages.append({
                    "role": "user",
                    "content": self._build_execution_context(action.code, result),
                })
            else:
                # No code and no answer — prompt to continue
                self.messages.append({"role": "assistant", "content": action.model_dump_json()})
                self.messages.append({
                    "role": "user",
                    "content": "Please continue. If the task is done, set final_answer. "
                    "Otherwise provide code to execute.",
                })

            yield self._emit(EventType.ROUND_FINISHED)

        if self.round_num >= self.settings.max_rounds and not self.answer and not self._cancelled:
            self.answer = f"Max rounds ({self.settings.max_rounds}) reached."
            yield self._emit(EventType.AGENT_ERROR, self.answer)

        yield self._emit(EventType.AGENT_FINISHED, self.answer)

    # ── helpers ─────────────────────────────────────────────────────────────

    def _emit(
        self,
        event_type: EventType,
        message: Optional[str] = None,
        **kwargs,
    ) -> AgentEvent:
        event = AgentEvent(
            type=event_type,
            run_id=self.run_id,
            session_id=self.session_id,
            round_num=self.round_num,
            message=message,
            **kwargs,
        )
        if self.store is not None:
            try:
                self.store.append_event(event)
            except Exception:
                logger.exception("Failed to persist event %s", event_type.value)
        return event

    def _handle_delegation(self, action: AgentAction) -> Generator[AgentEvent, None, None]:
        """Run a sub-agent for a delegated subtask."""
        try:
            request = DelegateRequest(**action.delegate)
        except Exception as e:
            yield self._emit(EventType.AGENT_ERROR, f"Invalid delegate request: {e}")
            self.messages.append({"role": "assistant", "content": action.model_dump_json()})
            self.messages.append({
                "role": "user",
                "content": f"Invalid delegate request: {e}. "
                "Use format {\"role\": \"statistician\", \"task\": \"...\"}",
            })
            return

        yield self._emit(
            EventType.SUBAGENT_STARTED,
            f"Delegating to {request.role}: {request.task[:100]}",
        )

        result = self.subagent.run(request)

        yield self._emit(
            EventType.SUBAGENT_FINISHED,
            f"Sub-agent ({request.role}) returned result",
        )

        # Feed sub-agent result back to the main agent
        self.messages.append({"role": "assistant", "content": action.model_dump_json()})
        self.messages.append({
            "role": "user",
            "content": (
                f"Sub-agent ({request.role}) completed the subtask. "
                f"Here is its result:\n\n{result}\n\n"
                "Use this result to continue your analysis. "
                "If the task is fully done, set final_answer."
            ),
        })

    def _should_reject_answer(self) -> bool:
        if not self.plan:
            return False
        pending = [s for s in self.plan.steps if not s.completed]
        if pending:
            logger.warning("Rejecting early answer: %d steps pending", len(pending))
            return True
        return False

    @staticmethod
    def _parse_plan(text: str) -> PlanState:
        """Parse a markdown checklist into a PlanState."""
        from insightforge.schema.models import PlanStep

        steps = []
        for i, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            # Match "- [ ] ..." or "- [x] ..." or "1. ..."
            import re

            m = re.match(r"^[-*]\s*\[( |x|X)\]\s*(.+)$", line)
            if m:
                completed = m.group(1).lower() == "x"
                steps.append(PlanStep(number=len(steps) + 1, description=m.group(2), completed=completed))
                continue
            m = re.match(r"^\d+\.\s+(.+)$", line)
            if m:
                steps.append(PlanStep(number=len(steps) + 1, description=m.group(1)))
        return PlanState(steps=steps, raw_text=text)

    @staticmethod
    def _build_execution_context(code: str, result: ExecutionResult) -> str:
        parts = [f"Code executed:\n```python\n{code}\n```"]
        if result.success:
            parts.append(f"Output:\n{result.truncated_output}")
            if result.images:
                parts.append(f"[{len(result.images)} image(s) generated]")
        else:
            parts.append(f"Error output:\n{result.truncated_output}")
        return "\n\n".join(parts)