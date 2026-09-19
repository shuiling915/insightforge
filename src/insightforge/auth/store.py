"""SQLite-backed user store with role-based permissions."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from insightforge.config import get_settings

ROLE_ADMIN = "admin"
ROLE_ANALYST = "analyst"
ROLE_VIEWER = "viewer"

ALL_ROLES = {ROLE_ADMIN, ROLE_ANALYST, ROLE_VIEWER}


@dataclass
class User:
    id: int
    username: str
    password_hash: str
    role: str
    created_at: int
    updated_at: int
    is_active: bool = True
    must_change_password: bool = False


class UserStore:
    """Persistent user storage backed by SQLite."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        settings = get_settings()
        self.db_path = db_path or str(Path(settings.workspace) / "users.db")
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'analyst',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    must_change_password INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # Add column if upgrading from old schema
            try:
                conn.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            conn.commit()
        self._ensure_default_admin()

    def _ensure_default_admin(self) -> None:
        """Create a default admin account if no users exist."""
        with self._conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if count == 0:
                from insightforge.auth.service import AuthService

                now = int(time.time())
                conn.execute(
                    "INSERT INTO users (username, password_hash, role, created_at, updated_at, is_active, must_change_password) "
                    "VALUES (?, ?, ?, ?, ?, 1, 1)",
                    ("admin", AuthService.hash_password("admin123"), ROLE_ADMIN, now, now),
                )
                conn.commit()

    def create(self, username: str, password_hash: str, role: str = ROLE_ANALYST) -> User:
        if role not in ALL_ROLES:
            raise ValueError(f"Invalid role: {role}")
        now = int(time.time())
        with self._lock:
            with self._conn() as conn:
                try:
                    cur = conn.execute(
                        "INSERT INTO users (username, password_hash, role, created_at, updated_at, is_active) "
                        "VALUES (?, ?, ?, ?, ?, 1)",
                        (username, password_hash, role, now, now),
                    )
                    conn.commit()
                except sqlite3.IntegrityError:
                    raise ValueError(f"Username already exists: {username}")
                user_id = cur.lastrowid
        return self.get(user_id)

    def get(self, user_id: int) -> Optional[User]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._row_to_user(row) if row else None

    def get_by_username(self, username: str) -> Optional[User]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
        return self._row_to_user(row) if row else None

    def list_users(self) -> List[User]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [self._row_to_user(r) for r in rows]

    def update_role(self, user_id: int, role: str) -> Optional[User]:
        if role not in ALL_ROLES:
            raise ValueError(f"Invalid role: {role}")
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE users SET role = ?, updated_at = ? WHERE id = ?",
                    (role, int(time.time()), user_id),
                )
                conn.commit()
        return self.get(user_id)

    def update_password(self, user_id: int, password_hash: str) -> Optional[User]:
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE users SET password_hash = ?, updated_at = ?, must_change_password = 0 WHERE id = ?",
                    (password_hash, int(time.time()), user_id),
                )
                conn.commit()
        return self.get(user_id)

    def set_active(self, user_id: int, is_active: bool) -> Optional[User]:
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?",
                    (1 if is_active else 0, int(time.time()), user_id),
                )
                conn.commit()
        return self.get(user_id)

    def delete(self, user_id: int) -> bool:
        with self._lock:
            with self._conn() as conn:
                cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
                conn.commit()
        return cur.rowcount > 0

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> User:
        return User(
            id=row["id"],
            username=row["username"],
            password_hash=row["password_hash"],
            role=row["role"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            is_active=bool(row["is_active"]),
            must_change_password=bool(row["must_change_password"]),
        )