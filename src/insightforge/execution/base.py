"""Abstract executor backend interface.

Production deployments MUST use a sandboxed backend (DockerExecutor).
LocalExecutor is provided for local development only and runs code in
the host process with NO isolation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from insightforge.schema.models import ExecutionResult


class ExecutorConfig:
    def __init__(
        self,
        workspace: str | Path,
        timeout: int = 300,
        docker_image: Optional[str] = None,
        memory_limit: Optional[str] = None,
        cpu_limit: Optional[float] = None,
        network: str = "none",
        prelude: str = "",
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.timeout = timeout
        self.docker_image = docker_image
        self.memory_limit = memory_limit
        self.cpu_limit = cpu_limit
        self.network = network
        # Python source prepended to every execution (or injected once at
        # kernel start for persistent backends).  Used to make built-in tools
        # such as describe_table / find_metrics available to agent code.
        self.prelude = prelude


class ExecutorBackend(ABC):
    """Interface for code execution backends."""

    @abstractmethod
    def start(self) -> None:
        """Start the execution environment (e.g. launch container)."""

    @abstractmethod
    def execute(self, code: str) -> ExecutionResult:
        """Execute Python code and return the result."""

    @abstractmethod
    def shutdown(self) -> None:
        """Tear down the execution environment."""

    @property
    @abstractmethod
    def is_running(self) -> bool: ...

    def __enter__(self) -> "ExecutorBackend":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.shutdown()