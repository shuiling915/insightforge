"""Basic tests for the safety checker and structured output parsing."""

import json

from insightforge.security.safety import CodeSafetyChecker


def test_safety_blocks_subprocess():
    checker = CodeSafetyChecker()
    report = checker.check("import subprocess\nsubprocess.run(['ls'])")
    assert not report.safe
    assert "subprocess" in report.message.lower()


def test_safety_blocks_eval():
    checker = CodeSafetyChecker()
    report = checker.check("x = eval('1+1')")
    assert not report.safe
    assert "eval" in report.message.lower()


def test_safety_allows_pandas():
    checker = CodeSafetyChecker()
    report = checker.check("import pandas as pd\ndf = pd.DataFrame({'a': [1,2,3]})")
    assert report.safe


def test_safety_allows_matplotlib():
    checker = CodeSafetyChecker()
    report = checker.check("import matplotlib.pyplot as plt\nplt.plot([1,2,3])")
    assert report.safe


def test_agent_action_schema_parses():
    from insightforge.schema.models import AgentAction

    action = AgentAction.model_validate({
        "thinking": "let me check the data",
        "code": "print('hello')",
        "final_answer": None,
        "plan_update": None,
    })
    assert action.thinking == "let me check the data"
    assert action.code == "print('hello')"
    assert action.final_answer is None