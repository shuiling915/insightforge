"""Factory for executor backends."""

from __future__ import annotations

from insightforge.config import Settings
from insightforge.execution.base import ExecutorBackend, ExecutorConfig


def create_executor(settings: Settings, workspace: str) -> ExecutorBackend:
    """Create an executor backend based on settings.executor_backend."""
    config = ExecutorConfig(
        workspace=workspace,
        timeout=settings.code_timeout,
        docker_image=settings.docker_image,
        memory_limit=settings.docker_memory_limit,
        cpu_limit=settings.docker_cpu_limit,
        network=settings.docker_network,
    )

    backend = settings.executor_backend.lower()
    if backend == "docker":
        from insightforge.execution.docker_executor import DockerExecutor

        return DockerExecutor(config)
    elif backend == "local":
        from insightforge.execution.local import LocalExecutor

        return LocalExecutor(config)
    else:
        raise ValueError(
            f"Unknown executor backend: {backend}. Use 'docker' or 'local'."
        )