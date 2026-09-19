"""CLI entry point for InsightForge."""

from __future__ import annotations

import argparse
import sys

from insightforge.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="insightforge", description="InsightForge data analysis agent")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="Start the API server")

    run_p = sub.add_parser("run", help="Run a one-off analysis task")
    run_p.add_argument("task", help="The analysis task to perform")
    run_p.add_argument("--model", default=None, help="Override the LLM model")
    run_p.add_argument(
        "--executor",
        choices=["local", "docker"],
        default=None,
        help="Override the executor backend",
    )

    eval_p = sub.add_parser("evaluate", help="Run the evaluation suite")
    eval_p.add_argument(
        "--cases",
        default=None,
        help="Path to a JSON file with test cases. If omitted, runs built-in cases.",
    )
    eval_p.add_argument(
        "--executor",
        choices=["local", "docker"],
        default=None,
        help="Override the executor backend",
    )

    args = parser.parse_args()

    if args.command == "serve":
        import uvicorn

        settings = get_settings()
        uvicorn.run(
            "insightforge.server.app:app",
            host=settings.host,
            port=settings.port,
            reload=False,
        )

    elif args.command == "run":
        import copy

        from insightforge.agent.loop import DataAnalysisAgent
        from insightforge.execution.factory import create_executor

        settings = get_settings()
        if args.model:
            settings = copy.deepcopy(settings)
            settings.model = args.model
        if args.executor:
            settings = copy.deepcopy(settings)
            settings.executor_backend = args.executor

        executor = create_executor(settings, settings.workspace)
        executor.start()
        try:
            agent = DataAnalysisAgent(settings=settings, executor=executor)
            for event in agent.run_stream(args.task):
                if event.message:
                    print(f"[{event.type.value}] {event.message}")
                if event.type.value == "code_executing" and event.code:
                    print(f"  code: {event.code[:100]}...")
                if event.type.value == "code_success" and event.result:
                    print(f"  output: {event.result.truncated_output[:200]}")
                if event.type.value == "code_failed":
                    print(f"  error: {event.message}")
                if event.type.value == "answer_finished":
                    print(f"\n=== ANSWER ===\n{event.message}")
        finally:
            executor.shutdown()

    elif args.command == "evaluate":
        import copy
        import json

        from insightforge.evaluation.harness import Evaluator, TestCase
        from insightforge.execution.factory import create_executor

        settings = get_settings()
        if args.executor:
            settings = copy.deepcopy(settings)
            settings.executor_backend = args.executor

        if args.cases:
            with open(args.cases) as f:
                raw = json.load(f)
            cases = [TestCase(**c) for c in raw]
        else:
            cases = _builtin_cases()

        executor = create_executor(settings, settings.workspace)
        executor.start()
        try:
            evaluator = Evaluator(settings=settings, executor=executor)
            report = evaluator.run_suite(cases)
            print(report.format())
            sys.exit(0 if report.pass_rate >= 0.8 else 1)
        finally:
            executor.shutdown()


def _builtin_cases():
    """Default evaluation cases for smoke testing."""
    from insightforge.evaluation.harness import TestCase

    return [
        TestCase(
            name="arithmetic_mean",
            task="Calculate the mean of [2, 4, 6, 8, 10]. Show the result.",
            expected_keywords=["mean", "6"],
            min_score=0.6,
        ),
        TestCase(
            name="list_operations",
            task="Compute the sum and max of [3, 1, 4, 1, 5, 9, 2, 6].",
            expected_keywords=["sum", "31", "max", "9"],
            min_score=0.6,
        ),
        TestCase(
            name="no_code_needed",
            task="What is 2 + 2? Answer directly.",
            expected_keywords=["4"],
            min_score=0.4,
        ),
    ]


if __name__ == "__main__":
    main()