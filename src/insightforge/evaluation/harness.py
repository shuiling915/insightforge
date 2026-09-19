"""Evaluation harness for measuring agent quality.

Enterprise production feature: before deploying a new model or prompt
version, run the evaluation suite to verify quality doesn't regress.

A TestCase has:
  - task: the user prompt
  - expected_keywords: substrings that MUST appear in the final answer
  - forbidden_keywords: substrings that must NOT appear
  - min_score: minimum acceptable score (0-1)

Scoring:
  - keyword_hit_rate: fraction of expected_keywords found
  - no_forbidden: 1.0 if no forbidden keywords, else 0.0
  - has_code: 1.0 if the agent executed at least one code block
  - completed: 1.0 if the agent produced a final_answer (not max-rounds)
  - final_score = 0.4 * keyword_hit_rate + 0.2 * no_forbidden
                + 0.2 * has_code + 0.2 * completed
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

from insightforge.agent.loop import DataAnalysisAgent
from insightforge.config import Settings
from insightforge.execution.base import ExecutorBackend
from insightforge.schema.models import EventType

logger = logging.getLogger(__name__)


@dataclass
class TestCase:
    name: str
    task: str
    expected_keywords: List[str] = field(default_factory=list)
    forbidden_keywords: List[str] = field(default_factory=list)
    min_score: float = 0.5

    def score(self, answer: str, executed_code: bool, completed: bool) -> float:
        answer_lower = answer.lower()

        # Keyword hit rate
        if self.expected_keywords:
            hits = sum(
                1 for kw in self.expected_keywords if kw.lower() in answer_lower
            )
            keyword_rate = hits / len(self.expected_keywords)
        else:
            keyword_rate = 1.0

        # Forbidden keywords
        no_forbidden = 1.0
        if self.forbidden_keywords:
            if any(kw.lower() in answer_lower for kw in self.forbidden_keywords):
                no_forbidden = 0.0

        has_code = 1.0 if executed_code else 0.0
        completed_score = 1.0 if completed else 0.0

        return (
            0.4 * keyword_rate
            + 0.2 * no_forbidden
            + 0.2 * has_code
            + 0.2 * completed_score
        )


@dataclass
class EvaluationResult:
    test_name: str
    score: float
    passed: bool
    answer: str
    executed_code: bool
    completed: bool
    rounds: int
    duration_ms: float
    error: Optional[str] = None


@dataclass
class EvaluationReport:
    results: List[EvaluationResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def pass_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.passed / self.total

    @property
    def avg_score(self) -> float:
        if self.total == 0:
            return 0.0
        return sum(r.score for r in self.results) / self.total

    @property
    def avg_rounds(self) -> float:
        if self.total == 0:
            return 0.0
        return sum(r.rounds for r in self.results) / self.total

    @property
    def avg_duration_ms(self) -> float:
        if self.total == 0:
            return 0.0
        return sum(r.duration_ms for r in self.results) / self.total

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": round(self.pass_rate, 4),
            "avg_score": round(self.avg_score, 4),
            "avg_rounds": round(self.avg_rounds, 2),
            "avg_duration_ms": round(self.avg_duration_ms, 1),
            "results": [
                {
                    "test_name": r.test_name,
                    "score": round(r.score, 4),
                    "passed": r.passed,
                    "rounds": r.rounds,
                    "duration_ms": round(r.duration_ms, 1),
                    "error": r.error,
                }
                for r in self.results
            ],
        }

    def format(self) -> str:
        lines = [
            "=" * 60,
            f"  InsightForge Evaluation Report",
            "=" * 60,
            f"  Total tests:   {self.total}",
            f"  Passed:        {self.passed}",
            f"  Failed:        {self.failed}",
            f"  Pass rate:     {self.pass_rate:.1%}",
            f"  Avg score:     {self.avg_score:.4f}",
            f"  Avg rounds:    {self.avg_rounds:.1f}",
            f"  Avg duration:  {self.avg_duration_ms:.0f} ms",
            "-" * 60,
        ]
        for r in self.results:
            status = "PASS" if r.passed else "FAIL"
            lines.append(
                f"  [{status}] {r.test_name}: score={r.score:.2f} "
                f"rounds={r.rounds} ({r.duration_ms:.0f}ms)"
            )
            if r.error:
                lines.append(f"          error: {r.error}")
        lines.append("=" * 60)
        return "\n".join(lines)


class Evaluator:
    """Runs a suite of test cases against the agent."""

    def __init__(
        self,
        settings: Settings,
        executor: ExecutorBackend,
        gateway=None,
    ) -> None:
        self.settings = settings
        self.executor = executor
        self.gateway = gateway

    def run_suite(self, cases: List[TestCase]) -> EvaluationReport:
        report = EvaluationReport()
        for case in cases:
            result = self.run_case(case)
            report.results.append(result)
            logger.info(
                "Test %s: score=%.2f %s",
                case.name,
                result.score,
                "PASS" if result.passed else "FAIL",
            )
        return report

    def run_case(self, case: TestCase) -> EvaluationResult:
        start = time.time()
        agent = DataAnalysisAgent(
            settings=self.settings,
            executor=self.executor,
            gateway=self.gateway,
            store=None,
        )

        executed_code = False
        completed = False
        error = None
        answer = ""

        try:
            for event in agent.run_stream(case.task):
                if event.type == EventType.CODE_SUCCESS:
                    executed_code = True
                if event.type == EventType.ANSWER_FINISHED:
                    completed = True
            answer = agent.answer or ""
        except Exception as e:
            error = str(e)
            answer = agent.answer or ""

        duration_ms = (time.time() - start) * 1000
        score = case.score(answer, executed_code, completed)
        passed = score >= case.min_score

        return EvaluationResult(
            test_name=case.name,
            score=score,
            passed=passed,
            answer=answer,
            executed_code=executed_code,
            completed=completed,
            rounds=agent.round_num,
            duration_ms=duration_ms,
            error=error,
        )