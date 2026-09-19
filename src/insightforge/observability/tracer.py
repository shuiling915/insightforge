"""Lightweight observability: structured tracing + Prometheus metrics.

For production, set INSIGHTFORGE_OBSERVABILITY_ENABLED=true and configure
Langfuse keys. The tracer wraps LLM calls and code executions with
span metadata.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Dict, Optional

from prometheus_client import Counter, Histogram

logger = logging.getLogger(__name__)

# ── Metrics ──────────────────────────────────────────────────────────────────
LLM_CALLS_TOTAL = Counter(
    "insightforge_llm_calls_total",
    "Total LLM calls",
    ["model", "status"],
)
LLM_LATENCY = Histogram(
    "insightforge_llm_latency_seconds",
    "LLM call latency",
    ["model"],
)
CODE_EXECUTIONS_TOTAL = Counter(
    "insightforge_code_executions_total",
    "Total code executions",
    ["status"],
)
CODE_EXECUTION_LATENCY = Histogram(
    "insightforge_code_execution_seconds",
    "Code execution latency",
)
RUNS_TOTAL = Counter(
    "insightforge_runs_total",
    "Total agent runs",
    ["status"],
)


class Tracer:
    """Minimal tracer that logs spans and updates metrics.

    In production this is where you'd integrate Langfuse / OpenTelemetry.
    """

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    @contextmanager
    def span(self, name: str, **attributes: Any):
        start = time.time()
        try:
            yield
            status = "ok"
        except Exception:
            status = "error"
            raise
        finally:
            elapsed = time.time() - start
            if self.enabled:
                logger.info(
                    "span=%s status=%s duration_ms=%.0f attrs=%s",
                    name,
                    status,
                    elapsed * 1000,
                    attributes,
                )

    def record_llm_call(self, model: str, status: str, latency_ms: float) -> None:
        LLM_CALLS_TOTAL.labels(model=model, status=status).inc()
        LLM_LATENCY.labels(model=model).observe(latency_ms / 1000)

    def record_code_execution(self, success: bool, latency_ms: float) -> None:
        CODE_EXECUTIONS_TOTAL.labels(status="ok" if success else "error").inc()
        CODE_EXECUTION_LATENCY.observe(latency_ms / 1000)

    def record_run(self, status: str) -> None:
        RUNS_TOTAL.labels(status=status).inc()