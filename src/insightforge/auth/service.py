"""Authentication service: password hashing, JWT tokens, role checks."""

from __future__ import annotations

import re
import secrets
import threading
import time
from typing import Optional, Set

import jwt
from passlib.context import CryptContext

from insightforge.auth.store import ROLE_ADMIN, ROLE_ANALYST, ROLE_VIEWER, User, UserStore
from insightforge.config import get_settings

_pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

# Default JWT TTL: 1 hour (shorter than 24h to limit stolen-token window)
DEFAULT_TOKEN_TTL = 3600


class AuthService:
    def __init__(self, store: Optional[UserStore] = None) -> None:
        self.store = store or UserStore()
        settings = get_settings()
        if settings.jwt_secret:
            self._secret = settings.jwt_secret
        else:
            # Only fall back to random when auth is not enforced (dev mode)
            self._secret = secrets.token_hex(32)
        # Token blacklist for logout / forced revocation
        self._blacklist: Set[str] = set()
        self._blacklist_lock = threading.Lock()

    # ── Password ──────────────────────────────────────────────────────────

    @staticmethod
    def hash_password(password: str) -> str:
        return _pwd_context.hash(password)

    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        return _pwd_context.verify(password, password_hash)

    @staticmethod
    def validate_password_strength(password: str, min_length: int = 8) -> Optional[str]:
        """Return an error message if password is too weak, else None."""
        if len(password) < min_length:
            return f"Password must be at least {min_length} characters"
        if not re.search(r"[A-Z]", password):
            return "Password must contain at least one uppercase letter"
        if not re.search(r"[a-z]", password):
            return "Password must contain at least one lowercase letter"
        if not re.search(r"\d", password):
            return "Password must contain at least one digit"
        return None

    # ── JWT ───────────────────────────────────────────────────────────────

    def create_token(self, user: User, ttl_seconds: int = DEFAULT_TOKEN_TTL) -> str:
        payload = {
            "sub": str(user.id),
            "username": user.username,
            "role": user.role,
            "iat": int(time.time()),
            "exp": int(time.time()) + ttl_seconds,
        }
        return jwt.encode(payload, self._secret, algorithm="HS256")

    def decode_token(self, token: str) -> Optional[dict]:
        try:
            return jwt.decode(token, self._secret, algorithms=["HS256"])
        except jwt.PyJWTError:
            return None

    def revoke_token(self, token: str) -> None:
        """Add a token to the blacklist so it can no longer be used."""
        with self._blacklist_lock:
            self._blacklist.add(token)

    def is_token_revoked(self, token: str) -> bool:
        with self._blacklist_lock:
            return token in self._blacklist

    # ── Auth flow ─────────────────────────────────────────────────────────

    def authenticate(self, username: str, password: str) -> Optional[User]:
        user = self.store.get_by_username(username)
        if user and user.is_active and self.verify_password(password, user.password_hash):
            return user
        return None

    def get_user_from_token(self, token: str) -> Optional[User]:
        if self.is_token_revoked(token):
            return None
        payload = self.decode_token(token)
        if not payload:
            return None
        user = self.store.get(int(payload["sub"]))
        if user and user.is_active:
            return user
        return None

    # ── Role checks ───────────────────────────────────────────────────────

    @staticmethod
    def has_role(user: User, role: str) -> bool:
        return user.role == role

    @staticmethod
    def is_admin(user: User) -> bool:
        return user.role == ROLE_ADMIN

    @staticmethod
    def can_run_analysis(user: User) -> bool:
        return user.role in (ROLE_ADMIN, ROLE_ANALYST)

    @staticmethod
    def can_view(user: User) -> bool:
        return user.role in (ROLE_ADMIN, ROLE_ANALYST, ROLE_VIEWER)

    @staticmethod
    def can_manage_users(user: User) -> bool:
        return user.role == ROLE_ADMIN