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
import re
import threading
import uuid
from typing import Generator, List, Optional

from insightforge.agent.context import ContextManager
from insightforge.agent.prompts import build_system_prompt
from insightforge.agent.subagent import DelegateRequest, SubAgent
from insightforge.config import Settings
from insightforge.data.metrics import MetricRegistry
from insightforge.data.registry import SchemaRegistry
from insightforge.execution.base import ExecutorBackend
from insightforge.gateway.llm import LLMError, LLMGateway
from insightforge.schema.models import (
    AgentAction,
    AgentEvent,
    EventType,
    ExecutionResult,
    PlanState,
)
from insightforge.security.safety import CodeQualityChecker, CodeSafetyChecker
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
        schema_registry: Optional[SchemaRegistry] = None,
        metric_registry: Optional[MetricRegistry] = None,
    ) -> None:
        self.settings = settings
        self.executor = executor
        self.gateway = gateway or LLMGateway(settings)
        self.safety = CodeSafetyChecker()
        self.quality = CodeQualityChecker(
            max_result_rows=getattr(settings, "max_result_rows", 1000)
        )
        self.store = store
        self.cancel_event = cancel_event or threading.Event()
        self.schema_registry = schema_registry
        self.metric_registry = metric_registry
        self.context_manager = ContextManager(
            max_context_tokens=getattr(settings, "context_window_tokens", 128000),
            keep_recent_rounds=getattr(settings, "context_keep_recent_rounds", 6),
            retrieve_top_k=getattr(settings, "context_retrieve_top_k", 4),
            similarity_threshold=getattr(settings, "context_similarity_threshold", 0.15),
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
        self._answer_rejections = 0
        self._max_answer_rejections = 2

    # ── public API ──────────────────────────────────────────────────────────

    def run(self, task: str) -> str:
        for _ in self.run_stream(task):
            pass
        return self.answer or "No answer generated."

    def run_stream(self, task: str) -> Generator[AgentEvent, None, None]:
        # Reset per-run state so the same agent instance can be reused safely.
        self.round_num = 0
        self.answer = None
        self.plan = None
        self.run_id = uuid.uuid4().hex
        self._token_estimate = 0
        self._answer_rejections = 0
        self._cancelled = False
        self._empty_round_count = 0

        yield self._emit(EventType.AGENT_STARTED, f"Starting: {task}")

        history_messages: List[dict] = []
        if self.store is not None and self.session_id:
            try:
                session_state = self.store.load(self.session_id)
                if session_state is not None and session_state.messages:
                    history_messages = session_state.messages
                    logger.info(
                        "Loaded %d historical messages for session %s",
                        len(history_messages),
                        self.session_id,
                    )
            except Exception:
                logger.exception("Failed to load session history for %s", self.session_id)

        if history_messages:
            self.messages = list(history_messages)
            self.messages.append({"role": "user", "content": f"Task: {task}"})
        else:
            schema_summary = ""
            if self.schema_registry is not None:
                schema_summary = self.schema_registry.get_summary()
            metric_summary = ""
            if self.metric_registry is not None:
                metric_summary = self.metric_registry.get_summary()
            self.messages = [
                {"role": "system", "content": build_system_prompt(schema_summary, metric_summary)},
                {"role": "user", "content": f"Task: {task}"},
            ]

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

            # Account for token budget: input messages + LLM output.
            input_tokens = self.context_manager.estimate_tokens(self.messages)
            output_tokens = len(action.model_dump_json()) // 4
            self._token_estimate += input_tokens + output_tokens
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
                new_plan = self._parse_plan(action.plan_update)
                if self.plan:
                    completed_descs = {
                        s.description.strip().lower()
                        for s in self.plan.steps
                        if s.completed
                    }
                    for step in new_plan.steps:
                        if step.description.strip().lower() in completed_descs:
                            step.mark_complete()
                self.plan = new_plan
                yield self._emit(EventType.PLAN_UPDATED, plan=self.plan)

            # Respond to cancellation promptly after the LLM returns.
            if self.cancel_event.is_set():
                self._cancelled = True
                yield self._emit(EventType.AGENT_CANCELLED, "Run cancelled by user.")
                break

            # 3. Execute code first so any accompanying final_answer is grounded
            #    in real execution output (not dropped when both fields are set).
            code_executed = False
            code_succeeded = False
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

                # Quality review (warnings only)
                quality = self.quality.check(action.code)
                yield self._emit(EventType.CODE_EXECUTING, "执行代码", code=action.code)
                result = self.executor.execute(action.code)
                code_executed = True

                if result.success:
                    code_succeeded = True
                    yield self._emit(
                        EventType.CODE_SUCCESS,
                        result.truncated_output,
                        result=result,
                    )
                    if self.plan and self.plan.current_step:
                        self.plan.current_step.mark_complete()
                        yield self._emit(EventType.PLAN_STEP_COMPLETED)
                else:
                    yield self._emit(
                        EventType.CODE_FAILED,
                        result.truncated_output,
                        result=result,
                    )

                self.messages.append({"role": "assistant", "content": action.model_dump_json()})
                self.messages.append({
                    "role": "user",
                    "content": self._build_execution_context(action.code, result, quality),
                })

            # 3b. Handle sub-agent delegation
            if action.delegate:
                yield from self._handle_delegation(action)
                continue

            # 4. Check for final answer (after code has run, if any).
            if action.final_answer:
                # If the LLM paired a final_answer with code that failed, the
                # answer is ungrounded — reject it and ask to retry.
                if code_executed and not code_succeeded:
                    self.messages.append({
                        "role": "user",
                        "content": (
                            "Your final_answer was paired with code that failed. "
                            "Please fix the code or revise your answer."
                        ),
                    })
                    continue

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

            # 5. No code / answer / delegate — detect stuck loops.
            if not action.code and not action.final_answer and not action.delegate:
                self._empty_round_count += 1
                if self._empty_round_count >= 3:
                    yield self._emit(
                        EventType.AGENT_ERROR,
                        "Agent produced 3 consecutive empty actions; aborting.",
                    )
                    break
                self.messages.append({"role": "assistant", "content": action.model_dump_json()})
                self.messages.append({
                    "role": "user",
                    "content": "Please continue. If the task is done, set final_answer. "
                    "Otherwise provide code to execute.",
                })
            else:
                self._empty_round_count = 0

            yield self._emit(EventType.ROUND_FINISHED)

        if self.round_num >= self.settings.max_rounds and not self.answer and not self._cancelled:
            self.answer = f"Max rounds ({self.settings.max_rounds}) reached."
            yield self._emit(EventType.AGENT_ERROR, self.answer)

        # Tear down the isolated sub-agent executor (if one was created).
        sub_executor = getattr(self, "_subagent_executor", None)
        if sub_executor is not None:
            try:
                sub_executor.shutdown()
            except Exception:
                logger.exception("Failed to shut down sub-agent executor")
            self._subagent_executor = None

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
            # Persist off the hot path so a slow/locked store cannot block the
            # event stream. Exceptions are logged inside the worker.
            def _persist(ev=event):
                try:
                    self.store.append_event(ev)
                except Exception:
                    logger.exception("Failed to persist event %s", event_type.value)

            threading.Thread(target=_persist, daemon=True).start()
        return event

    def _handle_delegation(self, action: AgentAction) -> Generator[AgentEvent, None, None]:
        """Run a specialist agent for a delegated subtask."""
        try:
            request = DelegateRequest(**action.delegate)
        except Exception as e:
            yield self._emit(EventType.AGENT_ERROR, f"Invalid delegate request: {e}")
            self.messages.append({"role": "assistant", "content": action.model_dump_json()})
            self.messages.append({
                "role": "user",
                "content": f"Invalid delegate request: {e}. "
                "Use format {\"role\": \"schema_explorer\", \"task\": \"...\"}",
            })
            return

        yield self._emit(
            EventType.SUBAGENT_STARTED,
            f"Delegating to {request.role}: {request.task[:100]}",
        )

        # Use a dedicated executor for the specialist so its code does not
        # pollute the orchestrator's kernel namespace.
        sub_executor = self._get_subagent_executor()
        self.subagent.executor = sub_executor
        result = self.subagent.run(request)

        yield self._emit(
            EventType.SUBAGENT_FINISHED,
            f"Specialist ({request.role}) returned result",
        )

        # A successful delegation advances the current plan step.
        if self.plan and self.plan.current_step:
            self.plan.current_step.mark_complete()
            yield self._emit(EventType.PLAN_STEP_COMPLETED)

        # Role-aware feedback to guide the orchestrator's next action.
        next_hint = self._delegation_next_hint(request.role)

        self.messages.append({"role": "assistant", "content": action.model_dump_json()})
        self.messages.append({
            "role": "user",
            "content": (
                f"Specialist ({request.role}) completed the subtask.\n\n"
                f"## Result from {request.role}\n{result}\n\n"
                f"## Next step\n{next_hint}"
            ),
        })

    @staticmethod
    def _delegation_next_hint(role: str) -> str:
        """Return a role-specific hint about what the orchestrator should do next."""
        hints = {
            "schema_explorer": (
                "Now delegate to **data_engineer** to run the actual analysis "
                "using the tables/metrics identified above."
            ),
            "data_engineer": (
                "Now delegate to **analyst** to interpret these execution results "
                "and extract business insights."
            ),
            "analyst": (
                "Review the analyst's insights. If confidence is high and all "
                "questions are answered, set **final_answer**. If anomalies were "
                "flagged, delegate back to **data_engineer** for re-checking. "
                "If charts are needed, delegate to **visualizer**."
            ),
            "visualizer": (
                "Charts have been created. If the analysis is complete, set "
                "**final_answer** summarizing the findings and referencing the "
                "chart files."
            ),
            "statistician": (
                "Statistical results are ready. If the analysis is complete, "
                "set **final_answer**."
            ),
            "data_cleaner": (
                "Data is cleaned. Now delegate to **data_engineer** to run "
                "the analysis on the cleaned data."
            ),
            "researcher": (
                "Exploration findings are ready. If sufficient, set "
                "**final_answer**. Otherwise delegate to **data_engineer** "
                "for deeper analysis."
            ),
        }
        return hints.get(
            role,
            "If the task is complete, set final_answer. Otherwise continue "
            "with the next step.",
        )

    def _get_subagent_executor(self) -> ExecutorBackend:
        """Lazily create (and reuse) an isolated executor for sub-agents.

        The executor shares the same workspace + prelude as the main executor
        but runs in its own kernel/container, so variable state from
        sub-agent code does not leak into the main agent's environment.
        """
        if getattr(self, "_subagent_executor", None) is None:
            from insightforge.execution.factory import create_executor

            config = self.executor.config
            self._subagent_executor = create_executor(
                self.settings,
                str(config.workspace),
                prelude=config.prelude,
            )
            self._subagent_executor.start()
        return self._subagent_executor

    def _should_reject_answer(self) -> bool:
        if not self.plan:
            return False
        if self._answer_rejections >= self._max_answer_rejections:
            logger.warning(
                "Allowing final answer after %d rejections (plan may be incomplete)",
                self._answer_rejections,
            )
            return False
        pending = [s for s in self.plan.steps if not s.completed]
        if pending:
            self._answer_rejections += 1
            logger.warning(
                "Rejecting early answer (%d/%d): %d steps pending",
                self._answer_rejections,
                self._max_answer_rejections,
                len(pending),
            )
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
            m = re.match(r"^[-*]\s*\[( |x|X)\]\s*(.+)$", line)
            if m:
                completed = m.group(1).lower() == "x"
                steps.append(PlanStep(number=len(steps) + 1, description=m.group(2), completed=completed))
                continue
            m = re.match(r"^\d+\.\s+(.+)$", line)
            if m:
                steps.append(PlanStep(number=len(steps) + 1, description=m.group(1)))
                continue
            m = re.match(r"^[-*]\s+(.+)$", line)
            if m:
                steps.append(PlanStep(number=len(steps) + 1, description=m.group(1)))
        return PlanState(steps=steps, raw_text=text)

    @staticmethod
    def _summarize_output(output: str, max_chars: int = 2000) -> str:
        if len(output) <= max_chars:
            return output
        row_count = output.count("\n")
        return f"{output[:max_chars]}\n\n... [truncated: {len(output)} chars, ~{row_count} lines]"

    @staticmethod
    def _build_execution_context(
        code: str,
        result: ExecutionResult,
        quality=None,
    ) -> str:
        parts = [f"Code executed:\n```python\n{code}\n```"]
        if result.success:
            parts.append(f"Output:\n{DataAnalysisAgent._summarize_output(result.truncated_output)}")
            if result.images:
                parts.append(f"[{len(result.images)} image(s) generated]")
        else:
            parts.append(f"Error output:\n{result.truncated_output}")
        if quality is not None and quality.issues:
            parts.append(quality.message)
        return "\n\n".join(parts)