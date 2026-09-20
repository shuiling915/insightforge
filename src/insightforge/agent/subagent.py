"""Sub-agent for multi-agent collaboration.

A SubAgent is a specialized, short-lived agent that the main agent
delegates a focused subtask to. It has:
  - An isolated message history (doesn't pollute the main agent's context)
  - A specialized system prompt for its role
  - Fewer max rounds (it should return quickly)
  - No final-answer guard (it returns a result summary to the main agent)

The main agent's action schema gains a `delegate` field:
  {
    "delegate": {
      "role": "statistician" | "visualizer" | "data_cleaner" | "researcher",
      "task": "specific subtask description"
    }
  }

When the main agent delegates, the SubAgent runs to completion and its
result is injected back into the main agent's context as a tool result.
"""

from __future__ import annotations

import copy
import logging
import uuid
from typing import List, Optional

from pydantic import BaseModel, Field

from insightforge.agent.prompts import DEFAULT_ROLE_PROMPT, ROLE_PROMPTS
from insightforge.config import Settings
from insightforge.execution.base import ExecutorBackend
from insightforge.gateway.llm import LLMGateway
from insightforge.schema.models import AgentAction, AgentEvent, EventType

logger = logging.getLogger(__name__)


# ── Role metadata ─────────────────────────────────────────────────────────────

ROLE_DESCRIPTIONS = {
    "schema_explorer": "Finds relevant tables, columns, and pre-defined metrics.",
    "data_engineer": "Writes and executes SQL/Python code for data extraction.",
    "analyst": "Interprets results and identifies business insights.",
    "visualizer": "Creates publication-quality charts as PNG files.",
    "statistician": "Computes precise statistical measures.",
    "data_cleaner": "Handles missing values, outliers, type conversions.",
    "researcher": "Explores data from multiple angles for patterns.",
}


class DelegateRequest(BaseModel):
    """A delegation request from the orchestrator to a specialist agent."""

    role: str = Field(
        default="data_engineer",
        description=(
            "Specialist role: schema_explorer, data_engineer, analyst, "
            "visualizer, statistician, data_cleaner, researcher."
        ),
    )
    task: str = Field(
        ...,
        min_length=1,
        description="The specific subtask for the specialist to perform.",
    )


class SubAgent:
    """A short-lived specialized agent that performs one subtask.

    It reuses the DataAnalysisAgent loop but with a role-specific prompt
    and reduced max_rounds. The result is returned as a text summary
    that the main agent can incorporate into its reasoning.
    """

    def __init__(
        self,
        settings: Settings,
        executor: ExecutorBackend,
        gateway: Optional[LLMGateway] = None,
        max_rounds: int = 8,
    ) -> None:
        self.settings = settings
        self.executor = executor
        self.gateway = gateway
        self.max_rounds = max_rounds
        self.subagent_id = uuid.uuid4().hex[:8]

    def run(
        self,
        request: DelegateRequest,
        event_callback=None,
    ) -> str:
        """Run the specialist agent and return a text summary of results.

        Args:
            request: The delegation request with role and task.
            event_callback: Optional callback(AgentEvent) for streaming.

        Returns:
            A text summary of the specialist's work and findings.
        """
        role_prompt = ROLE_PROMPTS.get(request.role, DEFAULT_ROLE_PROMPT)

        system_prompt = (
            f"{role_prompt}\n\n"
            f"# Your subtask\n{request.task}\n\n"
            f"# Instructions\n"
            f"- Work only on this subtask. Do not try to solve the bigger picture.\n"
            f"- Execute code to get real results (unless you are schema_explorer).\n"
            f"- When done, put your findings in final_answer as a concise summary.\n"
            f"- Include exact numbers where applicable.\n"
        )

        from insightforge.agent.loop import DataAnalysisAgent

        sub_settings = copy.deepcopy(self.settings)
        sub_settings.max_rounds = self.max_rounds

        agent = DataAnalysisAgent(
            settings=sub_settings,
            executor=self.executor,
            gateway=self.gateway,
            store=None,
        )

        original_build = None
        try:
            import insightforge.agent.loop as loop_mod

            original_build = loop_mod.build_system_prompt
            loop_mod.build_system_prompt = lambda schema_summary="", metric_summary="": system_prompt

            logger.info(
                "SubAgent %s running task for role=%s",
                self.subagent_id,
                request.role,
            )

            if event_callback:
                for event in agent.run_stream(request.task):
                    event_callback(event)
                result = agent.answer
            else:
                result = agent.run(request.task)

            logger.info(
                "SubAgent %s finished (%d rounds)",
                self.subagent_id,
                agent.round_num,
            )
            return result or "(Specialist produced no result.)"

        finally:
            if original_build is not None:
                import insightforge.agent.loop as loop_mod

                loop_mod.build_system_prompt = original_build