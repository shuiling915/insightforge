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

import logging
import uuid
from typing import List, Optional

from pydantic import BaseModel, Field

from insightforge.config import Settings
from insightforge.execution.base import ExecutorBackend
from insightforge.gateway.llm import LLMGateway
from insightforge.schema.models import AgentAction, AgentEvent, EventType

logger = logging.getLogger(__name__)


# ── Specialized system prompts for sub-agent roles ────────────────────────────

ROLE_PROMPTS = {
    "statistician": """You are a statistics specialist sub-agent.
Your job is to compute precise statistical measures on the data.
Focus on accuracy: means, medians, correlations, distributions, significance tests.
Return a concise summary of the statistics you computed with exact numbers.""",

    "visualizer": """You are a data visualization specialist sub-agent.
Your job is to create clear, publication-quality charts using matplotlib (Agg backend).
Save all figures to the workspace as PNG files.
Return a list of the chart files you created and what each shows.""",

    "data_cleaner": """You are a data cleaning specialist sub-agent.
Your job is to handle missing values, outliers, type conversions, and data quality issues.
Always work on a copy of the data, never modify the original.
Return a summary of what you cleaned and the resulting dataset shape.""",

    "researcher": """You are a research specialist sub-agent.
Your job is to explore the data from multiple angles and find interesting patterns.
Try different groupings, filters, and comparisons.
Return a bullet list of the most interesting findings with supporting numbers.""",
}

DEFAULT_ROLE_PROMPT = """You are a specialist sub-agent.
Focus on the specific subtask assigned to you.
Return a concise, factual summary of your findings."""


class DelegateRequest(BaseModel):
    """A delegation request from the main agent to a sub-agent."""

    role: str = Field(
        default="researcher",
        description="Specialist role: statistician, visualizer, data_cleaner, researcher.",
    )
    task: str = Field(
        ...,
        min_length=1,
        description="The specific subtask for the sub-agent to perform.",
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
        """Run the sub-agent and return a text summary of results.

        Args:
            request: The delegation request with role and task.
            event_callback: Optional callback(AgentEvent) for streaming.

        Returns:
            A text summary of the sub-agent's work and findings.
        """
        role_prompt = ROLE_PROMPTS.get(request.role, DEFAULT_ROLE_PROMPT)

        # Build a focused system prompt for the sub-agent
        system_prompt = (
            f"{role_prompt}\n\n"
            f"# Your subtask\n{request.task}\n\n"
            f"# Instructions\n"
            f"- Work only on this subtask. Do not try to solve the bigger picture.\n"
            f"- Execute code to get real results.\n"
            f"- When done, put your findings in final_answer as a concise summary.\n"
            f"- Include exact numbers where applicable.\n"
        )

        # Use a copy of settings with reduced rounds
        import copy

        from insightforge.agent.loop import DataAnalysisAgent

        sub_settings = copy.deepcopy(self.settings)
        sub_settings.max_rounds = self.max_rounds

        agent = DataAnalysisAgent(
            settings=sub_settings,
            executor=self.executor,
            gateway=self.gateway,
            store=None,
        )

        # Override the system prompt
        original_build = None
        try:
            import insightforge.agent.loop as loop_mod

            original_build = loop_mod.build_system_prompt
            loop_mod.build_system_prompt = lambda: system_prompt

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
            return result or "(Sub-agent produced no result.)"

        finally:
            if original_build is not None:
                import insightforge.agent.loop as loop_mod

                loop_mod.build_system_prompt = original_build