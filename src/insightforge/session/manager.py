"""Session manager: orchestrates sessions, executors, and persistence."""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path
from typing import Dict, Optional

from insightforge.config import Settings
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

    def get_or_create_executor(self, session_id: str) -> ExecutorBackend:
        if session_id not in self._active:
            workspace = self._workspace_for(session_id)
            Path(workspace).mkdir(parents=True, exist_ok=True)
            self._seed_workspace(workspace)
            executor = create_executor(self.settings, workspace)
            executor.start()
            self._active[session_id] = executor
            logger.info("Started executor for session %s", session_id)
        return self._active[session_id]

    def create_session_id(self) -> str:
        return uuid.uuid4().hex

    def shutdown_session(self, session_id: str) -> None:
        executor = self._active.pop(session_id, None)
        if executor:
            executor.shutdown()
            logger.info("Shut down executor for session %s", session_id)

    def shutdown_all(self) -> None:
        for session_id in list(self._active):
            self.shutdown_session(session_id)