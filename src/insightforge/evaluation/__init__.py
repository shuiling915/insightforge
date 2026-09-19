"""Evaluation package for InsightForge.

Provides:
  - TestCase: a task + ground truth + scoring rubric
  - Evaluator: runs the agent against test cases and scores outputs
  - EvaluationReport: aggregates metrics (success rate, avg score, etc.)
"""

from insightforge.evaluation.harness import (
    EvaluationReport,
    EvaluationResult,
    Evaluator,
    TestCase,
)

__all__ = ["TestCase", "Evaluator", "EvaluationResult", "EvaluationReport"]