"""Session manager: orchestrates sessions, executors, and persistence."""

from __future__ import annotations

import logging
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Optional

import yaml

from insightforge.config import Settings
from insightforge.data.metrics import MetricRegistry
from insightforge.data.prelude import build_tool_prelude
from insightforge.data.registry import SchemaRegistry
from insightforge.execution.base import ExecutorBackend
from insightforge.execution.factory import create_executor
from insightforge.session.store import SessionStoreBackend, SQLiteSessionStore

logger = logging.getLogger(__name__)


class SessionManager:
    """Manages active sessions and their execution sandboxes."""

    def __init__(self, settings: Settings, store: Optional[SessionStoreBackend] = None) -> None:
        self.settings = settings
        self.store = store or SQLiteSessionStore()
        self._active: Dict[str, ExecutorBackend] = {}
        self._registries: Dict[str, SchemaRegistry] = {}
        self._metric_registries: Dict[str, MetricRegistry] = {}
        self._last_access: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._cleanup_thread: Optional[threading.Thread] = None
        self._start_cleanup_loop()

    def _workspace_for(self, session_id: str) -> str:
        return str(Path(self.settings.workspace) / session_id)

    def _seed_workspace(self, session_workspace: str) -> None:
        """Copy sample database files from the root workspace into the session dir."""
        root = Path(self.settings.workspace)
        if not root.exists():
            return
        for seed_file in root.glob("*.db"):
            dest = Path(session_workspace) / seed_file.name
            if not dest.exists():
                try:
                    shutil.copy2(seed_file, dest)
                    logger.info("Seeded %s into session workspace", seed_file.name)
                except OSError:
                    pass
        # Copy schema descriptions if present
        for desc_file in root.glob("*descriptions*.yaml"):
            dest = Path(session_workspace) / desc_file.name
            if not dest.exists():
                try:
                    shutil.copy2(desc_file, dest)
                except OSError:
                    pass
        # Copy metric definitions if present
        for metric_file in root.glob("*metrics*.yaml"):
            dest = Path(session_workspace) / metric_file.name
            if not dest.exists():
                try:
                    shutil.copy2(metric_file, dest)
                except OSError:
                    pass

    def _load_descriptions(self, workspace: str) -> dict:
        """Load and merge all *_descriptions.yaml files in the workspace."""
        merged = {"tables": {}}
        workspace_path = Path(workspace)
        for yaml_file in sorted(workspace_path.glob("*descriptions*.yaml")):
            try:
                with open(yaml_file, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                tables = data.get("tables", {})
                if isinstance(tables, dict):
                    for tname, tinfo in tables.items():
                        if tname not in merged["tables"]:
                            merged["tables"][tname] = {}
                        if isinstance(tinfo, dict):
                            merged["tables"][tname].update(tinfo)
                        else:
                            merged["tables"][tname]["description"] = str(tinfo)
            except Exception:
                logger.exception("Failed to load schema descriptions from %s", yaml_file)
        return merged if merged["tables"] else {}

    def _load_metrics(self, workspace: str) -> dict:
        """Load and merge all *_metrics.yaml files in the workspace."""
        merged: Dict = {"metrics": {}}
        workspace_path = Path(workspace)
        for yaml_file in sorted(workspace_path.glob("*metrics*.yaml")):
            try:
                with open(yaml_file, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                metrics = data.get("metrics", {})
                if isinstance(metrics, dict):
                    merged["metrics"].update(metrics)
            except Exception:
                logger.exception("Failed to load metrics from %s", yaml_file)
        return merged if merged["metrics"] else {}

    def get_or_create_executor(self, session_id: str) -> ExecutorBackend:
        with self._lock:
            self._last_access[session_id] = time.time()
            if session_id not in self._active:
                workspace = self._workspace_for(session_id)
                Path(workspace).mkdir(parents=True, exist_ok=True)
                self._seed_workspace(workspace)

                # Build registries BEFORE the executor so we can inject the
                # tool prelude (describe_table / find_metrics / ...) into the
                # sandbox.  This replaces the fragile regex-based interception.
                descriptions = self._load_descriptions(workspace)
                schema_registry = SchemaRegistry(workspace, descriptions)
                self._registries[session_id] = schema_registry

                metrics_data = self._load_metrics(workspace)
                metric_registry = MetricRegistry(metrics_data)
                self._metric_registries[session_id] = metric_registry

                # P0-2: validate metric definitions against the actual schema
                # and surface mismatches as warnings.
                validation_warnings = metric_registry.validate(schema_registry)
                for w in validation_warnings:
                    logger.warning("Metric validation: %s", w)

                prelude = build_tool_prelude(schema_registry, metric_registry)
                executor = create_executor(self.settings, workspace, prelude=prelude)
                executor.start()
                self._active[session_id] = executor
                logger.info("Started executor for session %s", session_id)
            return self._active[session_id]

    def get_schema_registry(self, session_id: str) -> Optional[SchemaRegistry]:
        with self._lock:
            return self._registries.get(session_id)

    def get_metric_registry(self, session_id: str) -> Optional[MetricRegistry]:
        with self._lock:
            return self._metric_registries.get(session_id)

    def create_session_id(self) -> str:
        return uuid.uuid4().hex

    def touch(self, session_id: str) -> None:
        """Update last-access time for an existing session."""
        with self._lock:
            if session_id in self._active:
                self._last_access[session_id] = time.time()

    def shutdown_session(self, session_id: str) -> None:
        with self._lock:
            executor = self._active.pop(session_id, None)
            self._registries.pop(session_id, None)
            self._metric_registries.pop(session_id, None)
            self._last_access.pop(session_id, None)
        if executor:
            executor.shutdown()
            logger.info("Shut down executor for session %s", session_id)

    def shutdown_all(self) -> None:
        self._stop_event.set()
        for session_id in list(self._active):
            self.shutdown_session(session_id)
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=5)

    # ── background TTL cleanup ─────────────────────────────────────────────

    def _start_cleanup_loop(self) -> None:
        interval = getattr(self.settings, "session_cleanup_interval_seconds", 300)

        def _loop() -> None:
            while not self._stop_event.wait(interval):
                try:
                    self._cleanup_expired()
                except Exception:
                    logger.exception("Session cleanup tick failed")

        self._cleanup_thread = threading.Thread(
            target=_loop, name="session-cleanup", daemon=True
        )
        self._cleanup_thread.start()
        logger.info("Session cleanup loop started (interval=%ds)", interval)

    def _cleanup_expired(self) -> None:
        ttl = getattr(self.settings, "session_ttl_seconds", 1800)
        now = time.time()
        expired: list[str] = []
        with self._lock:
            for sid, last in self._last_access.items():
                if now - last > ttl:
                    expired.append(sid)
        if expired:
            logger.info("Expiring %d idle session(s)", len(expired))
            for sid in expired:
                self.shutdown_session(sid)