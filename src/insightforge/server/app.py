"""FastAPI application with SSE streaming, API key auth, and rate limiting.

Production features added:
  - Event Store persistence (run_events table) with Last-Event-ID replay
  - Collaborative cancellation via POST /runs/{run_id}/cancel
  - SSE heartbeats to keep proxies alive
  - RunRegistry for managing active runs and their cancel signals
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Deque, Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import generate_latest
from pydantic import BaseModel

from insightforge.agent.loop import DataAnalysisAgent
from insightforge.auth.service import AuthService
from insightforge.auth.store import ROLE_ADMIN, User, UserStore
from insightforge.config import Settings, get_settings
from insightforge.schema.models import (
    AgentEvent,
    EventType,
    RunRequest,
    RunResponse,
    SessionState,
)
from insightforge.session.manager import SessionManager

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 15


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._requests: Dict[str, Deque[float]] = defaultdict(deque)
        # Login attempt tracking per IP (for brute-force protection)
        self._login_attempts: Dict[str, Deque[float]] = defaultdict(deque)
        self.max_login_attempts = 5

    def check(self, key: str) -> bool:
        now = time.time()
        dq = self._requests[key]
        while dq and now - dq[0] > 60:
            dq.popleft()
        if len(dq) >= self.per_minute:
            return False
        dq.append(now)
        return True

    def check_login(self, ip: str) -> bool:
        """Return True if login is allowed (under brute-force threshold)."""
        now = time.time()
        dq = self._login_attempts[ip]
        # Window of 5 minutes
        while dq and now - dq[0] > 300:
            dq.popleft()
        return len(dq) < self.max_login_attempts

    def record_login_failure(self, ip: str) -> None:
        self._login_attempts[ip].append(time.time())


class RunRegistry:
    """Tracks active runs so they can be cancelled."""

    def __init__(self) -> None:
        self._runs: Dict[str, Dict] = {}
        self._lock = threading.Lock()

    def register(self, run_id: str, agent: DataAnalysisAgent, cancel_event: threading.Event) -> None:
        with self._lock:
            self._runs[run_id] = {"agent": agent, "cancel_event": cancel_event}

    def cancel(self, run_id: str) -> bool:
        with self._lock:
            entry = self._runs.get(run_id)
        if entry is None:
            return False
        entry["cancel_event"].set()
        return True

    def is_active(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._runs

    def unregister(self, run_id: str) -> None:
        with self._lock:
            self._runs.pop(run_id, None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.require_api_key and not settings.api_key:
        raise RuntimeError("INSIGHTFORGE_REQUIRE_API_KEY=true but no API key set.")

    user_store = UserStore()
    auth_service = AuthService(user_store)

    session_manager = SessionManager(settings)
    app.state.settings = settings
    app.state.session_manager = session_manager
    app.state.rate_limiter = RateLimiter(settings.rate_limit_per_minute)
    app.state.run_registry = RunRegistry()
    app.state.auth_service = auth_service
    app.state.user_store = user_store

    logging.basicConfig(level=logging.INFO)
    logger.info("InsightForge starting on %s:%s", settings.host, settings.port)
    logger.info("Auth %s (default admin: admin / admin123)",
                "required" if settings.require_auth else "disabled")
    yield
    session_manager.shutdown_all()
    logger.info("InsightForge shut down.")


app = FastAPI(title="InsightForge", version="0.1.0", lifespan=lifespan)


def get_settings_dep() -> Settings:
    return get_settings()


def get_current_user(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    settings: Settings = Depends(get_settings_dep),
) -> Optional[User]:
    """Extract and validate the current user.

    Always attempts to validate a token if one is provided (so admin
    endpoints work even when require_auth=False). Only raises 401 when
    require_auth=True and no valid token is present.

    Token may be passed via Authorization: Bearer header or ?token= query
    param (for EventSource which doesn't support custom headers).
    """
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1]
    elif "token" in request.query_params:
        token = request.query_params["token"]

    if token:
        auth_service: AuthService = request.app.state.auth_service
        user = auth_service.get_user_from_token(token)
        if user:
            return user
        # Token present but invalid
        if settings.require_auth:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return None

    # No token provided
    if settings.require_auth:
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    return None


def require_admin(user: Optional[User] = Depends(get_current_user)) -> User:
    if user is None:
        raise HTTPException(status_code=403, detail="Admin role required")
    if not AuthService.is_admin(user):
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


def require_analyst(user: Optional[User] = Depends(get_current_user)) -> Optional[User]:
    if user is None:
        return None
    if not AuthService.can_run_analysis(user):
        raise HTTPException(status_code=403, detail="Insufficient permissions to run analysis")
    return user


def check_rate_limit(
    request: Request,
    user: Optional[User] = Depends(get_current_user),
    settings: Settings = Depends(get_settings_dep),
) -> None:
    limiter: RateLimiter = request.app.state.rate_limiter
    if user is not None:
        key = f"user:{user.id}"
    else:
        key = request.client.host if request.client else "anonymous"
    if not limiter.check(key):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")


STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": "0.1.0"}


@app.get("/metrics")
def metrics():
    from fastapi.responses import Response

    return Response(content=generate_latest(), media_type="text/plain")


# ── Auth models ──────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    role: str = "analyst"


class UpdateRoleRequest(BaseModel):
    role: str


class UpdatePasswordRequest(BaseModel):
    password: str


def _user_to_dict(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "is_active": user.is_active,
        "must_change_password": user.must_change_password,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }


# ── Auth endpoints ───────────────────────────────────────────────────────

@app.post("/auth/login")
def login(req: LoginRequest, request: Request):
    limiter: RateLimiter = request.app.state.rate_limiter
    client_ip = request.client.host if request.client else "unknown"

    if not limiter.check_login(client_ip):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")

    auth_service: AuthService = request.app.state.auth_service
    user = auth_service.authenticate(req.username, req.password)
    if not user:
        limiter.record_login_failure(client_ip)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = auth_service.create_token(user)
    return {"token": token, "user": _user_to_dict(user)}


@app.get("/auth/me")
def get_me(user: Optional[User] = Depends(get_current_user)):
    if user is None:
        return {"authenticated": False}
    return {"authenticated": True, "user": _user_to_dict(user)}


@app.post("/auth/register", dependencies=[Depends(require_admin)])
def register_user(req: RegisterRequest, request: Request):
    settings: Settings = request.app.state.settings
    user_store: UserStore = request.app.state.user_store
    pw_err = AuthService.validate_password_strength(req.password, settings.min_password_length)
    if pw_err:
        raise HTTPException(status_code=400, detail=pw_err)
    try:
        user = user_store.create(
            username=req.username,
            password_hash=AuthService.hash_password(req.password),
            role=req.role,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _user_to_dict(user)


@app.get("/admin/users", dependencies=[Depends(require_admin)])
def list_users(request: Request):
    user_store: UserStore = request.app.state.user_store
    return {"users": [_user_to_dict(u) for u in user_store.list_users()]}


@app.put("/admin/users/{user_id}/role", dependencies=[Depends(require_admin)])
def update_user_role(user_id: int, req: UpdateRoleRequest, request: Request):
    user_store: UserStore = request.app.state.user_store
    try:
        user = user_store.update_role(user_id, req.role)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return _user_to_dict(user)


@app.put("/admin/users/{user_id}/password", dependencies=[Depends(require_admin)])
def update_user_password(user_id: int, req: UpdatePasswordRequest, request: Request):
    settings: Settings = request.app.state.settings
    user_store: UserStore = request.app.state.user_store
    pw_err = AuthService.validate_password_strength(req.password, settings.min_password_length)
    if pw_err:
        raise HTTPException(status_code=400, detail=pw_err)
    user = user_store.update_password(user_id, AuthService.hash_password(req.password))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "password updated"}


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


@app.post("/auth/change-password")
def change_password(
    req: ChangePasswordRequest,
    request: Request,
    user: User = Depends(get_current_user),
):
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    settings: Settings = request.app.state.settings
    auth_service: AuthService = request.app.state.auth_service
    user_store: UserStore = request.app.state.user_store

    if not auth_service.verify_password(req.old_password, user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    pw_err = AuthService.validate_password_strength(req.new_password, settings.min_password_length)
    if pw_err:
        raise HTTPException(status_code=400, detail=pw_err)
    user_store.update_password(user.id, AuthService.hash_password(req.new_password))
    return {"status": "password changed"}


@app.post("/auth/logout")
def logout(request: Request, user: Optional[User] = Depends(get_current_user)):
    auth_service: AuthService = request.app.state.auth_service
    token = None
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1]
    elif "token" in request.query_params:
        token = request.query_params["token"]
    if token:
        auth_service.revoke_token(token)
    return {"status": "logged out"}


@app.put("/admin/users/{user_id}/active", dependencies=[Depends(require_admin)])
def set_user_active(user_id: int, is_active: bool, request: Request):
    user_store: UserStore = request.app.state.user_store
    user = user_store.set_active(user_id, is_active)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return _user_to_dict(user)


@app.delete("/admin/users/{user_id}", dependencies=[Depends(require_admin)])
def delete_user(user_id: int, request: Request, admin: User = Depends(require_admin)):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    user_store: UserStore = request.app.state.user_store
    if not user_store.delete(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "deleted"}


@app.post("/runs", response_model=RunResponse, dependencies=[Depends(check_rate_limit), Depends(require_analyst)])
def create_run(
    req: RunRequest,
    request: Request,
):
    settings: Settings = request.app.state.settings
    session_manager: SessionManager = request.app.state.session_manager

    session_id = req.session_id or session_manager.create_session_id()
    run_id = session_manager.create_session_id()

    from datetime import datetime

    existing = session_manager.store.load(session_id)
    if existing is not None:
        existing.updated_at = datetime.now()
        existing.model = req.model or existing.model
        session_manager.store.save(existing)
    else:
        state = SessionState(
            session_id=session_id,
            model=req.model or settings.model,
            messages=[],
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        session_manager.store.save(state)

    return RunResponse(run_id=run_id, session_id=session_id, status="started")


@app.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, request: Request):
    registry: RunRegistry = request.app.state.run_registry
    if registry.cancel(run_id):
        return {"status": "cancelling", "run_id": run_id}
    raise HTTPException(status_code=404, detail=f"Run {run_id} not found or already finished")


@app.get("/runs/{run_id}/stream", dependencies=[Depends(require_analyst)])
def stream_run(
    run_id: str,
    task: str,
    session_id: str | None = None,
    model: str | None = None,
    last_event_id: Optional[int] = Header(default=None, alias="Last-Event-ID"),
    request: Request = None,
):
    settings: Settings = request.app.state.settings
    if len(task) > settings.max_task_length:
        raise HTTPException(
            status_code=413,
            detail=f"Task too long (max {settings.max_task_length} characters)",
        )
    session_manager: SessionManager = request.app.state.session_manager
    registry: RunRegistry = request.app.state.run_registry
    store = session_manager.store

    effective_session = session_id or session_manager.create_session_id()
    effective_model = model or settings.model

    run_settings = copy.deepcopy(settings)
    run_settings.model = effective_model

    # If client provides Last-Event-ID, replay persisted events first
    replay_events: list[AgentEvent] = []
    if last_event_id is not None:
        replay_events = store.get_events(run_id, since_seq=last_event_id)
        logger.info("Replaying %d events for run %s (since seq %d)", len(replay_events), run_id, last_event_id)

    # If we already have a terminal event in replay, just replay and exit
    terminal_types = {EventType.AGENT_FINISHED, EventType.AGENT_ERROR, EventType.AGENT_CANCELLED}
    has_terminal = any(e.type in terminal_types for e in replay_events)

    cancel_event = threading.Event()

    def event_generator():
        # 1. Replay persisted events
        for ev in replay_events:
            yield ev.to_sse()

        if has_terminal:
            return

        # 2. Run the agent live
        executor = session_manager.get_or_create_executor(effective_session)
        agent = DataAnalysisAgent(
            settings=run_settings,
            executor=executor,
            store=store,
            cancel_event=cancel_event,
        )
        # Override run_id so replay and live events share the same ID
        agent.run_id = run_id
        agent.session_id = effective_session
        registry.register(run_id, agent, cancel_event)

        last_emit = time.time()
        try:
            for event in agent.run_stream(task):
                yield event.to_sse()
                last_emit = time.time()
        except Exception as e:
            logger.exception("Run failed")
            err_event = AgentEvent(
                type=EventType.AGENT_ERROR,
                run_id=run_id,
                error=str(e),
            )
            try:
                store.append_event(err_event)
            except Exception:
                pass
            yield err_event.to_sse()
        finally:
            registry.unregister(run_id)
            try:
                from datetime import datetime

                state = SessionState(
                    session_id=effective_session,
                    model=effective_model,
                    messages=agent.messages,
                    plan=agent.plan,
                )
                store.save(state)
            except Exception:
                logger.exception("Failed to save session state")

    def event_generator_with_heartbeat():
        gen = event_generator()
        last_emit = time.time()
        buffer = []
        done = False

        while not done:
            try:
                chunk = next(gen)
                yield chunk
                last_emit = time.time()
            except StopIteration:
                done = True
            # Emit heartbeat if no data for a while
            if not done and time.time() - last_emit > HEARTBEAT_INTERVAL:
                yield ": heartbeat\n\n"
                last_emit = time.time()

    return StreamingResponse(
        event_generator_with_heartbeat(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/sessions")
def list_sessions(request: Request):
    session_manager: SessionManager = request.app.state.session_manager
    sessions = session_manager.store.list_sessions()
    sessions = sorted(sessions, key=lambda s: s.updated_at if hasattr(s, 'updated_at') else 0, reverse=True)
    result = []
    for s in sessions:
        user_msgs = [m for m in s.messages if m.get("role") == "user"]
        title = user_msgs[0]["content"] if user_msgs else (s.messages[0]["content"] if s.messages else "新会话")
        if len(title) > 50:
            title = title[:50] + "..."
        result.append({
            "id": s.session_id,
            "session_id": s.session_id,
            "title": title,
            "created_at": s.created_at.isoformat() if hasattr(s.created_at, 'isoformat') else str(s.created_at),
            "updated_at": s.updated_at.isoformat() if hasattr(s.updated_at, 'isoformat') else str(s.updated_at),
            "message_count": len(s.messages),
            "event_count": len(s.messages),
        })
    return result


@app.get("/sessions/{session_id}")
def get_session(session_id: str, request: Request):
    session_manager: SessionManager = request.app.state.session_manager
    store = session_manager.store
    events = store.get_events_by_session(session_id)
    event_list = []
    for e in events:
        d = e.model_dump(mode="json", exclude_none=True)
        d["type"] = e.type.value if hasattr(e.type, 'value') else str(e.type)
        event_list.append(d)
    return {
        "session_id": session_id,
        "events": event_list,
    }


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str, request: Request):
    session_manager: SessionManager = request.app.state.session_manager
    session_manager.shutdown_session(session_id)
    session_manager.store.delete(session_id)
    return {"status": "deleted"}


@app.on_event("startup")
def _add_cors():
    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key", "Last-Event-ID"],
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self';"
    )
    return response