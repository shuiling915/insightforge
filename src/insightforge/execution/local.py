"""Local executor for DEVELOPMENT ONLY.

Runs code in a Jupyter kernel in the host process. No isolation.
Never use this in production with untrusted input.
"""

from __future__ import annotations

import queue
from typing import Optional

from jupyter_client import KernelManager

from insightforge.execution.base import ExecutorBackend, ExecutorConfig
from insightforge.schema.models import ExecutionResult


class LocalExecutor(ExecutorBackend):
    def __init__(self, config: ExecutorConfig) -> None:
        self.config = config
        self._km: Optional[KernelManager] = None
        self._kc = None
        self._started = False

    @property
    def is_running(self) -> bool:
        return self._started and self._km is not None

    def start(self) -> None:
        if self._started:
            return
        self.config.workspace.mkdir(parents=True, exist_ok=True)
        self._km = KernelManager(kernel_name="python3")
        self._km.start_kernel(cwd=str(self.config.workspace))
        self._kc = self._km.client()
        self._kc.start_channels()
        self._kc.wait_for_ready(timeout=60)
        # Pre-configure matplotlib for headless image output
        self._execute_silent(
            "import warnings; warnings.filterwarnings('ignore')\n"
            "import matplotlib; matplotlib.use('Agg')\n"
            "import matplotlib.pyplot as plt; plt.ioff()\n"
        )
        # Inject built-in tool functions (describe_table, find_metrics, ...)
        if self.config.prelude:
            self._execute_silent(self.config.prelude)
        self._started = True

    def _drain(self) -> None:
        if not self._kc:
            return
        while True:
            try:
                self._kc.get_iopub_msg(timeout=0.1)
            except queue.Empty:
                break

    def _execute_silent(self, code: str) -> None:
        if not self._kc:
            return
        self._kc.execute(code)
        while True:
            try:
                msg = self._kc.get_shell_msg(timeout=30)
                if msg["msg_type"] == "execute_reply":
                    break
            except queue.Empty:
                break
        self._drain()

    def execute(self, code: str) -> ExecutionResult:
        import time

        if not self._kc:
            return ExecutionResult(error="Kernel not started", success=False)

        self._drain()
        result = ExecutionResult()
        msg_id = self._kc.execute(code)
        start = time.time()

        while True:
            try:
                msg = self._kc.get_iopub_msg(timeout=self.config.timeout)
                if msg.get("parent_header", {}).get("msg_id") != msg_id:
                    continue
                msg_type = msg["msg_type"]
                content = msg["content"]

                if msg_type == "stream":
                    if content["name"] == "stdout":
                        result.stdout += content["text"]
                    elif content["name"] == "stderr":
                        result.stderr += content["text"]
                elif msg_type in ("execute_result", "display_data"):
                    data = content.get("data", {})
                    if "text/plain" in data:
                        result.stdout += data["text/plain"] + "\n"
                    for mime in ("image/png", "image/jpeg", "image/svg+xml"):
                        if mime in data:
                            result.images.append({"mime": mime, "data": data[mime]})
                elif msg_type == "error":
                    result.error = "\n".join(content.get("traceback", []))
                    result.success = False
                elif msg_type == "status" and content["execution_state"] == "idle":
                    break
            except queue.Empty:
                result.error = f"Timeout after {self.config.timeout}s"
                result.success = False
                break
            except Exception as e:
                result.error = str(e)
                result.success = False
                break

        result.execution_time_ms = (time.time() - start) * 1000
        return result

    def shutdown(self) -> None:
        if self._kc:
            self._kc.stop_channels()
        if self._km:
            self._km.shutdown_kernel(now=True)
        self._started = False