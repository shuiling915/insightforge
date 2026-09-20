"""Test the hybrid context manager with semantic retrieval."""

import sys
sys.path.insert(0, "src")

from insightforge.agent.context import ContextManager


def make_round(thinking: str, output: str, round_idx: int) -> list:
    return [
        {"role": "assistant", "content": f'{{"thinking": "{thinking}", "code": "..."}}'},
        {"role": "user", "content": f"Code executed.\nOutput: {output}"},
    ]


def test_no_prune_when_under_budget():
    cm = ContextManager(max_context_tokens=128000, keep_recent_rounds=2)
    messages = [
        {"role": "system", "content": "You are an analyst."},
        {"role": "user", "content": "analyze sales data"},
    ]
    messages += make_round("checking data", "sales table loaded", 1)
    result, stats = cm.manage(messages)
    assert result == messages, "Under budget should return messages unchanged"
    assert not stats.pruned
    print("PASS: no prune when under budget")


def test_recent_rounds_kept_intact():
    cm = ContextManager(max_context_tokens=500, keep_recent_rounds=2, retrieve_top_k=1)
    messages = [
        {"role": "system", "content": "S" * 100},
        {"role": "user", "content": "current task about sales"},
    ]
    for i in range(5):
        messages += make_round(f"round {i} thinking", f"round {i} output", i)

    result, stats = cm.manage(messages)
    assert stats.pruned, "Should prune when over budget"

    # Check the last 2 rounds are fully present (assistant+user = 2 msgs each)
    result_text = " ".join(m["content"] for m in result if isinstance(m.get("content"), str))
    assert "round 4 thinking" in result_text, "Most recent round must be intact"
    assert "round 3 thinking" in result_text, "Second most recent round must be intact"
    print("PASS: recent rounds kept intact")


def test_semantic_retrieval():
    cm = ContextManager(max_context_tokens=500, keep_recent_rounds=1, retrieve_top_k=2)
    messages = [
        {"role": "system", "content": "S" * 100},
        {"role": "user", "content": "compare monthly revenue by product category"},
    ]
    # Old rounds: some relevant (revenue/category), some not (churn/users)
    messages += make_round("calculate monthly revenue", "revenue by month", 0)
    messages += make_round("product category breakdown", "category A B C", 1)
    messages += make_round("user churn analysis", "churn rate 5%", 2)
    messages += make_round("active users count", "10000 users", 3)
    # Recent round
    messages += make_round("loading data", "data loaded", 4)

    result, stats = cm.manage(messages)
    assert stats.pruned
    result_text = " ".join(m["content"] for m in result if isinstance(m.get("content"), str))

    # Relevant old rounds should be retrieved (full text present)
    assert "calculate monthly revenue" in result_text, "Relevant round about revenue should be retrieved"
    assert "product category breakdown" in result_text, "Relevant round about category should be retrieved"
    # Irrelevant rounds should only appear as summaries (not full thinking text)
    # (summaries extract thinking but the output part won't be fully there)
    print(f"PASS: semantic retrieval — retrieved {stats.retrieved_rounds} rounds")


def test_unrelated_rounds_summarized():
    cm = ContextManager(max_context_tokens=500, keep_recent_rounds=1, retrieve_top_k=1)
    messages = [
        {"role": "system", "content": "S" * 100},
        {"role": "user", "content": "sales analysis"},
    ]
    for i in range(4):
        messages += make_round(f"unrelated topic {i}", f"unrelated output {i}", i)
    messages += make_round("recent work", "recent output", 4)

    result, stats = cm.manage(messages)
    result_text = " ".join(m["content"] for m in result if isinstance(m.get("content"), str))
    # Should have a condensed summary section
    assert "Summary of previous rounds" in result_text
    print("PASS: unrelated rounds summarized")


def test_jaccard_similarity():
    cm = ContextManager()
    a = cm._ngram_set("monthly revenue by product category")
    b = cm._ngram_set("calculate revenue per category monthly")
    c = cm._ngram_set("user churn rate analysis")
    sim_ab = cm._jaccard(a, b)
    sim_ac = cm._jaccard(a, c)
    assert sim_ab > sim_ac, "Similar texts should have higher Jaccard score"
    print(f"PASS: Jaccard similarity works (sim_ab={sim_ab:.3f} > sim_ac={sim_ac:.3f})")


def test_config_defaults():
    from insightforge.config import Settings
    settings = Settings()
    assert settings.context_retrieve_top_k == 4
    assert settings.context_similarity_threshold == 0.15
    print("PASS: config defaults present")


if __name__ == "__main__":
    test_no_prune_when_under_budget()
    test_recent_rounds_kept_intact()
    test_semantic_retrieval()
    test_unrelated_rounds_summarized()
    test_jaccard_similarity()
    test_config_defaults()
    print("\nAll context manager tests passed!")