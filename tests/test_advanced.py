"""Tests for context management, sub-agent delegation, and evaluation."""

from insightforge.agent.context import ContextManager
from insightforge.schema.models import AgentAction, EventType


def test_context_manager_no_prune_when_under_budget():
    cm = ContextManager(max_context_tokens=100000, reserve_tokens=1000)
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
    ]
    pruned, stats = cm.manage(messages)
    assert not stats.pruned
    assert len(pruned) == 2


def test_context_manager_prunes_when_over_budget():
    cm = ContextManager(max_context_tokens=500, reserve_tokens=100, keep_recent_rounds=1)
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
    ]
    for i in range(10):
        messages.append({"role": "assistant", "content": f'{{"thinking": "round {i}"}}'})
        messages.append({"role": "user", "content": f"output {i} " * 20})

    pruned, stats = cm.manage(messages)
    assert stats.pruned
    assert pruned[0]["role"] == "system"
    assert pruned[1]["content"] == "task"
    has_summary = any(
        "Summary of previous rounds" in m.get("content", "") for m in pruned
    )
    assert has_summary


def test_agent_action_supports_delegate():
    action = AgentAction(
        thinking="need stats",
        delegate={"role": "statistician", "task": "compute mean"},
    )
    assert action.delegate is not None
    assert action.delegate["role"] == "statistician"


def test_event_types_exist():
    assert EventType.CONTEXT_PRUNED.value == "context_pruned"
    assert EventType.SUBAGENT_STARTED.value == "subagent_started"
    assert EventType.SUBAGENT_FINISHED.value == "subagent_finished"


def test_evaluation_scoring():
    from insightforge.evaluation.harness import TestCase

    case = TestCase(
        name="test",
        task="calc mean",
        expected_keywords=["mean", "3.0"],
        min_score=0.5,
    )
    score = case.score("The mean is 3.0", executed_code=True, completed=True)
    assert score == 1.0

    score = case.score("wrong answer", executed_code=True, completed=True)
    assert score < 1.0