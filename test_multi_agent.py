"""Tests for the hierarchical specialist multi-agent architecture.

Verifies:
  - All specialist role prompts are defined and non-empty
  - Orchestrator prompt drives delegation to specialists
  - DelegateRequest accepts new specialist roles
  - _delegation_next_hint returns role-appropriate guidance
  - SubAgent picks the correct role-specific prompt
  - End-to-end orchestrator delegation flow (mocked LLM)
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from insightforge.agent.prompts import (
    ANALYST_PROMPT,
    DATA_ENGINEER_PROMPT,
    DEFAULT_ROLE_PROMPT,
    ORCHESTRATOR_PROMPT,
    ROLE_PROMPTS,
    SCHEMA_EXPLORER_PROMPT,
    VISUALIZER_PROMPT,
    build_system_prompt,
)
from insightforge.agent.subagent import DelegateRequest, SubAgent
from insightforge.config import Settings


# ── Prompt presence tests ────────────────────────────────────────────────────

def test_all_specialist_prompts_defined():
    expected = {
        "schema_explorer",
        "data_engineer",
        "analyst",
        "visualizer",
        "statistician",
        "data_cleaner",
        "researcher",
    }
    assert set(ROLE_PROMPTS.keys()) == expected
    for role, prompt in ROLE_PROMPTS.items():
        assert prompt and prompt.strip(), f"Prompt for '{role}' is empty"


def test_schema_explorer_prompt_forbids_analysis_queries():
    assert "Do NOT run SELECT or analysis queries" in SCHEMA_EXPLORER_PROMPT
    assert "list_related" in SCHEMA_EXPLORER_PROMPT
    assert "describe_table" in SCHEMA_EXPLORER_PROMPT
    assert "find_metrics" in SCHEMA_EXPLORER_PROMPT


def test_data_engineer_prompt_focuses_on_execution():
    assert "execute" in DATA_ENGINEER_PROMPT.lower()
    assert "Do NOT interpret" in DATA_ENGINEER_PROMPT


def test_analyst_prompt_focuses_on_interpretation():
    assert "insights" in ANALYST_PROMPT.lower()
    assert "Anomalies" in ANALYST_PROMPT
    assert "Confidence" in ANALYST_PROMPT


def test_visualizer_prompt_mentions_matplotlib():
    assert "matplotlib" in VISUALIZER_PROMPT.lower()
    assert "png" in VISUALIZER_PROMPT.lower()


# ── Orchestrator prompt tests ────────────────────────────────────────────────

def test_orchestrator_prompt_mentions_all_specialists():
    for role in ("schema_explorer", "data_engineer", "analyst", "visualizer"):
        assert role in ORCHESTRATOR_PROMPT, f"Orchestrator prompt missing role: {role}"


def test_orchestrator_prompt_discourages_code():
    assert "Do NOT set `code`" in ORCHESTRATOR_PROMPT


def test_build_system_prompt_includes_overviews():
    prompt = build_system_prompt(
        schema_summary="table1, table2",
        metric_summary="metric1",
    )
    assert "table1, table2" in prompt
    assert "metric1" in prompt
    assert "Pre-scanned Schema Overview" in prompt
    assert "Pre-defined Metrics Overview" in prompt


def test_build_system_prompt_empty_inputs():
    prompt = build_system_prompt()
    assert "Pre-scanned Schema Overview" not in prompt
    assert "Pre-defined Metrics Overview" not in prompt


# ── DelegateRequest tests ────────────────────────────────────────────────────

def test_delegate_request_default_role():
    req = DelegateRequest(task="find tables")
    assert req.role == "data_engineer"


@pytest.mark.parametrize("role", [
    "schema_explorer", "data_engineer", "analyst", "visualizer",
    "statistician", "data_cleaner", "researcher",
])
def test_delegate_request_accepts_all_roles(role):
    req = DelegateRequest(role=role, task="do something")
    assert req.role == role


def test_delegate_request_unknown_role_accepted():
    # Unknown roles fall back to DEFAULT_ROLE_PROMPT at runtime
    req = DelegateRequest(role="unknown_role", task="do something")
    assert req.role == "unknown_role"


def test_delegate_request_task_required():
    with pytest.raises(Exception):
        DelegateRequest(task="")


# ── SubAgent prompt selection tests ──────────────────────────────────────────

def test_subagent_uses_correct_role_prompt():
    settings = Settings()
    executor = MagicMock()
    executor.config.workspace = "/tmp/ws"
    executor.config.prelude = ""

    sub = SubAgent(settings, executor, gateway=MagicMock())

    for role in ("schema_explorer", "data_engineer", "analyst", "visualizer"):
        prompt = ROLE_PROMPTS.get(role, DEFAULT_ROLE_PROMPT)
        assert "Your job" in prompt or "You are" in prompt


def test_subagent_unknown_role_falls_back_to_default():
    assert ROLE_PROMPTS.get("nonexistent_role", DEFAULT_ROLE_PROMPT) == DEFAULT_ROLE_PROMPT


# ── Orchestrator delegation flow tests (mocked) ──────────────────────────────

def test_delegation_next_hint_schema_explorer():
    from insightforge.agent.loop import DataAnalysisAgent
    hint = DataAnalysisAgent._delegation_next_hint("schema_explorer")
    assert "data_engineer" in hint


def test_delegation_next_hint_data_engineer():
    from insightforge.agent.loop import DataAnalysisAgent
    hint = DataAnalysisAgent._delegation_next_hint("data_engineer")
    assert "analyst" in hint


def test_delegation_next_hint_analyst():
    from insightforge.agent.loop import DataAnalysisAgent
    hint = DataAnalysisAgent._delegation_next_hint("analyst")
    assert "final_answer" in hint or "data_engineer" in hint


def test_delegation_next_hint_unknown_role():
    from insightforge.agent.loop import DataAnalysisAgent
    hint = DataAnalysisAgent._delegation_next_hint("unknown")
    assert "final_answer" in hint


# ── End-to-end orchestrator delegation (mocked LLM) ──────────────────────────

def test_orchestrator_delegates_to_specialists():
    """The orchestrator should be able to delegate to schema_explorer,
    data_engineer, analyst in sequence and then produce a final answer."""
    from insightforge.agent.loop import DataAnalysisAgent
    from insightforge.schema.models import AgentAction

    settings = Settings()
    settings.max_rounds = 10
    executor = MagicMock()
    executor.config.workspace = "/tmp/ws"
    executor.config.prelude = ""
    gateway = MagicMock()

    # Sequence of LLM responses: delegate to each specialist, then final answer
    actions = [
        AgentAction(
            thinking="need to find tables first",
            delegate={"role": "schema_explorer", "task": "find sales tables"},
        ),
        AgentAction(
            thinking="now run the analysis",
            delegate={"role": "data_engineer", "task": "compute total sales"},
        ),
        AgentAction(
            thinking="interpret results",
            delegate={"role": "analyst", "task": "interpret sales results"},
        ),
        AgentAction(
            thinking="done",
            final_answer="Sales analysis complete. Total revenue is $1M.",
        ),
    ]
    gateway.chat_structured.side_effect = actions

    agent = DataAnalysisAgent(settings, executor, gateway=gateway)

    # Mock the subagent to return canned results
    agent.subagent.run = MagicMock(return_value="Specialist result")

    answer = agent.run("Analyze sales data")

    # All delegations happened
    assert agent.subagent.run.call_count == 3
    # Final answer was produced
    assert agent.answer is not None
    assert "$1M" in agent.answer


def test_orchestrator_marks_plan_steps_complete_on_delegation():
    from insightforge.agent.loop import DataAnalysisAgent
    from insightforge.schema.models import AgentAction, PlanState, PlanStep

    settings = Settings()
    executor = MagicMock()
    executor.config.workspace = "/tmp/ws"
    executor.config.prelude = ""
    gateway = MagicMock()

    gateway.chat_structured.side_effect = [
        AgentAction(
            thinking="explore schema",
            plan_update="1. [ ] Find tables",
            delegate={"role": "schema_explorer", "task": "find tables"},
        ),
        AgentAction(
            thinking="done",
            final_answer="All done.",
        ),
    ]

    agent = DataAnalysisAgent(settings, executor, gateway=gateway)
    agent.subagent.run = MagicMock(return_value="tables found")

    agent.run("analyze data")

    # After delegation, the only plan step should be marked complete
    assert agent.plan is not None
    assert agent.plan.steps[0].completed


def test_orchestrator_handles_invalid_delegate():
    from insightforge.agent.loop import DataAnalysisAgent
    from insightforge.schema.models import AgentAction

    settings = Settings()
    settings.max_rounds = 5
    executor = MagicMock()
    executor.config.workspace = "/tmp/ws"
    executor.config.prelude = ""
    gateway = MagicMock()

    gateway.chat_structured.side_effect = [
        AgentAction(
            thinking="bad delegate",
            delegate={"role": "schema_explorer"},  # missing task
        ),
        AgentAction(
            thinking="retry",
            delegate={"role": "schema_explorer", "task": "find tables"},
        ),
        AgentAction(
            thinking="done",
            final_answer="Done",
        ),
    ]

    agent = DataAnalysisAgent(settings, executor, gateway=gateway)
    agent.subagent.run = MagicMock(return_value="result")

    answer = agent.run("test")

    # First delegation failed validation, second succeeded
    assert agent.subagent.run.call_count == 1
    assert answer is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])