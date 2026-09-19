"""Session persistence backends.

Abstract interface + SQLite implementation. For multi-instance
deployments, implement PostgresSessionStore using the same interface.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from insightforge.schema.models import AgentEvent, SessionState


class SessionStoreBackend(ABC):
    @abstractmethod
    def save(self, session: SessionState) -> None: ...

    @abstractmethod
    def load(self, session_id: str) -> Optional[SessionState]: ...

    @abstractmethod
    def delete(self, session_id: str) -> None: ...

    @abstractmethod
    def list_sessions(self) -> List[SessionState]: ...

    # ── Event Store ───────────────────────────────────────────────────────
    @abstractmethod
    def append_event(self, event: AgentEvent) -> None: ...

    @abstractmethod
    def get_events(self, run_id: str, since_seq: int = -1) -> List[AgentEvent]: ...

    @abstractmethod
    def next_seq(self, run_id: str) -> int: ...


class SQLiteSessionStore(SessionStoreBackend):
    """Single-file SQLite store. Good for single-instance deployments."""

    def __init__(self, db_path: str = "./sessions.db") -> None:
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_events (
                    run_id TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    seq INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, seq)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_run_events_run_id ON run_events(run_id)"
            )
            try:
                conn.execute("ALTER TABLE run_events ADD COLUMN session_id TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_run_events_session_id ON run_events(session_id)"
            )
            conn.commit()

    # ── Sessions ──────────────────────────────────────────────────────────

    def save(self, session: SessionState) -> None:
        session.update_timestamp()
        data = session.model_dump_json()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (session_id, data, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    data = excluded.data,
                    updated_at = excluded.updated_at
                """,
                (session.session_id, data, session.created_at.isoformat(), session.updated_at.isoformat()),
            )
            conn.commit()

    def load(self, session_id: str) -> Optional[SessionState]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if not row:
            return None
        return SessionState.model_validate_json(row[0])

    def delete(self, session_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            conn.commit()

    def list_sessions(self) -> List[SessionState]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM sessions ORDER BY updated_at DESC"
            ).fetchall()
        return [SessionState.model_validate_json(r[0]) for r in rows]

    # ── Event Store ───────────────────────────────────────────────────────

    def next_seq(self, run_id: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) FROM run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return row[0] + 1

    def append_event(self, event: AgentEvent) -> None:
        if event.seq == 0:
            event.seq = self.next_seq(event.run_id)
        data = event.model_dump_json(exclude_none=True)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO run_events (run_id, session_id, seq, type, data, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.run_id,
                    event.session_id,
                    event.seq,
                    event.type.value,
                    data,
                    event.timestamp.isoformat(),
                ),
            )
            conn.commit()

    def get_events(self, run_id: str, since_seq: int = -1) -> List[AgentEvent]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq ASC",
                (run_id, since_seq),
            ).fetchall()
        return [AgentEvent.model_validate_json(r[0]) for r in rows]

    def get_events_by_session(self, session_id: str) -> List[AgentEvent]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM run_events WHERE session_id = ? ORDER BY created_at ASC, seq ASC",
                (session_id,),
            ).fetchall()
        return [AgentEvent.model_validate_json(r[0]) for r in rows]