"""Docker-sandboxed code executor.

Each session gets its own ephemeral container with:
  - Memory + CPU limits
  - Network disabled by default
  - Workspace mounted READ-ONLY (databases cannot be modified)
  - A separate writable scratch dir for outputs (charts, temp files)
  - Auto-destroy on shutdown / timeout

This is the PRODUCTION backend. Unlike LocalExecutor, generated code
cannot escape to the host filesystem or network.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

from insightforge.execution.base import ExecutorBackend, ExecutorConfig
from insightforge.schema.models import ExecutionResult

logger = logging.getLogger(__name__)

# Bootstrap script that starts a Jupyter kernel inside the container
# and sets up matplotlib for headless rendering.
_KERNEL_SETUP = """
import json, sys, os, warnings, base64, io, shutil
warnings.filterwarnings('ignore')
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
plt.ioff()
# Copy read-only database files into the writable scratch dir so
# sqlite can create journal files without modifying the originals.
for f in os.listdir('/workspace'):
    if f.endswith('.db'):
        src = os.path.join('/workspace', f)
        dst = os.path.join('/scratch', f)
        if not os.path.exists(dst):
            shutil.copy2(src, dst)
os.chdir('/scratch')
print('KERNEL_READY', flush=True)
"""


class DockerExecutor(ExecutorBackend):
    def __init__(self, config: ExecutorConfig) -> None:
        self.config = config
        self._client = None
        self._container = None
        self._container_id: Optional[str] = None
        self._started = False

    @property
    def is_running(self) -> bool:
        return self._started and self._container is not None

    def start(self) -> None:
        if self._started:
            return
        try:
            import docker
        except ImportError as e:
            raise RuntimeError(
                "docker package is required for DockerExecutor. "
                "Install with: pip install docker"
            ) from e

        self._client = docker.from_env()
        name = f"insightforge-{uuid.uuid4().hex[:12]}"

        workspace = str(self.config.workspace)
        # Ensure workspace exists
        self.config.workspace.mkdir(parents=True, exist_ok=True)

        # Create a writable scratch directory for this session's outputs
        scratch_dir = self.config.workspace.parent / f"{self.config.workspace.name}_scratch"
        scratch_dir.mkdir(parents=True, exist_ok=True)

        host_config_kwargs = {}
        if self.config.memory_limit:
            host_config_kwargs["mem_limit"] = self.config.memory_limit
        if self.config.cpu_limit:
            # docker-py uses nano_cpus: 1 CPU = 1e9
            host_config_kwargs["nano_cpus"] = int(self.config.cpu_limit * 1e9)
        host_config_kwargs["network_mode"] = self.config.network
        # Drop all capabilities and run as non-root for extra isolation
        host_config_kwargs["cap_drop"] = ["ALL"]
        host_config_kwargs["security_opt"] = ["no-new-privileges:true"]

        try:
            self._container = self._client.containers.run(
                image=self.config.docker_image,
                name=name,
                detach=True,
                tty=True,
                stdin_open=True,
                working_dir="/scratch",
                user="1000:1000",
                volumes={
                    workspace: {"bind": "/workspace", "mode": "ro"},
                    str(scratch_dir): {"bind": "/scratch", "mode": "rw"},
                },
                host_config=self._client.api.create_host_config(**host_config_kwargs),
                entrypoint=["python", "-c", _KERNEL_SETUP],
                command=[],
            )
            self._container_id = self._container.id
            logger.info("Started sandbox container %s", name)
        except Exception as e:
            logger.error("Failed to start sandbox container: %s", e)
            raise

        # Wait for the kernel-ready marker
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                logs = self._container.logs(tail=20).decode()
                if "KERNEL_READY" in logs:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("Sandbox kernel did not become ready within 60s")

        # Write the tool prelude once to the writable /scratch volume so every
        # execution can import it cheaply instead of re-parsing 100+ KB inline.
        if self.config.prelude:
            prelude_path = self.config.workspace.parent / f"{self.config.workspace.name}_scratch" / "_prelude.py"
            prelude_path.write_text(self.config.prelude, encoding="utf-8")

        self._started = True

    def execute(self, code: str) -> ExecutionResult:
        if not self._container:
            return ExecutionResult(error="Container not started", success=False)

        result = ExecutionResult()
        start = time.time()

        # Wrap user code with output capture and image extraction.
        # The tool prelude is written to /scratch/_prelude.py once at start.
        prelude_import = ""
        if self.config.prelude:
            prelude_import = "exec(open('/scratch/_prelude.py').read())\n"

        wrapped = (
            "import sys, io, base64, json\n"
            "_buf_out, _buf_err = io.StringIO(), io.StringIO()\n"
            "_old_out, _old_err = sys.stdout, sys.stderr\n"
            "sys.stdout, sys.stderr = _buf_out, _buf_err\n"
            "_imgs = []\n"
            + prelude_import
            + "try:\n"
            f"{_indent(code, 1)}\n"
            "    import matplotlib.pyplot as plt\n"
            "    for _fig_num in plt.get_fignums():\n"
            "        _fig = plt.figure(_fig_num)\n"
            "        _bio = io.BytesIO()\n"
            "        _fig.savefig(_bio, format='png', bbox_inches='tight')\n"
            "        _imgs.append(base64.b64encode(_bio.getvalue()).decode())\n"
            "    plt.close('all')\n"
            "except Exception as _e:\n"
            "    import traceback\n"
            "    sys.stderr.write(traceback.format_exc())\n"
            "finally:\n"
            "    sys.stdout, sys.stderr = _old_out, _old_err\n"
            "print(json.dumps({'stdout': _buf_out.getvalue(), 'stderr': _buf_err.getvalue(), 'images': _imgs}))\n"
        )

        try:
            exit_code, output = self._container.exec_run(
                cmd=["python", "-c", wrapped],
                workdir="/scratch",
                demux=False,
            )
            text = output.decode(errors="replace")
            # The last line should be our JSON envelope
            lines = text.strip().splitlines()
            if lines:
                try:
                    envelope = json.loads(lines[-1])
                    result.stdout = envelope.get("stdout", "")
                    result.stderr = envelope.get("stderr", "")
                    for img_b64 in envelope.get("images", []):
                        result.images.append({"mime": "image/png", "data": img_b64})
                    if result.stderr:
                        result.success = False
                        result.error = result.stderr
                except json.JSONDecodeError:
                    # Fallback: treat whole output as stdout
                    result.stdout = text
        except Exception as e:
            result.error = str(e)
            result.success = False

        result.execution_time_ms = (time.time() - start) * 1000

        # Safety: kill + recreate container if execution was too slow
        # (prevents resource exhaustion from runaway code)
        if result.execution_time_ms > self.config.timeout * 1000 * 0.9:
            logger.warning(
                "Execution near timeout (%.0fms), recycling container",
                result.execution_time_ms,
            )
            self._recycle_container()

        return result

    def _recycle_container(self) -> None:
        try:
            if self._container:
                self._container.remove(force=True)
        except Exception:
            pass
        self._container = None
        self._started = False
        self.start()

    def shutdown(self) -> None:
        if self._container:
            try:
                self._container.remove(force=True)
                logger.info("Removed sandbox container %s", self._container_id)
            except Exception as e:
                logger.warning("Failed to remove container: %s", e)
        # Clean up the writable scratch directory
        scratch_dir = self.config.workspace.parent / f"{self.config.workspace.name}_scratch"
        if scratch_dir.exists():
            try:
                shutil.rmtree(scratch_dir, ignore_errors=True)
            except Exception:
                pass
        self._container = None
        self._started = False


def _indent(code: str, level: int) -> str:
    pad = "    " * level
    return "\n".join(pad + line if line.strip() else line for line in code.splitlines())