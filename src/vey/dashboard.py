"""Single-owner dashboard. No executor, model, core credentials or write endpoints."""

import asyncio
import json
import re
import secrets
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text

from vey.api_common import install_errors
from vey.configuration_client import ConfigurationClient
from vey.db import database
from vey.domain import VeyError
from vey.evaluation.reader import EvaluationReader
from vey.operations_report import operations_status
from vey.replay import export_snapshot
from vey.security import safe_value

COOKIE = "__Host-vey_dashboard"
STATUSES = {
    "queued",
    "control_queued",
    "running",
    "succeeded",
    "failed",
    "partial",
    "awaiting_confirmation",
    "cancelled",
    "unknown",
}
ASSETS = Path(__file__).with_name("dashboard_assets")


class DashboardSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VEY_DASHBOARD_", extra="ignore")
    database_url: SecretStr
    password_file: Path
    public_origin: str
    session_seconds: int = Field(default=900, ge=60, le=3600)
    eval_database_url: SecretStr | None = None
    operations_report_dir: Path | None = None
    configuration_token_file: Path | None = None
    configuration_socket: str = "/run/vey/executor.sock"

    @model_validator(mode="after")
    def validate_settings(self):
        url = urlsplit(self.public_origin)
        local = url.hostname in {"localhost", "127.0.0.1"}
        if (url.scheme != "https" and not (local and url.scheme == "http")) or (
            not url.hostname
            or url.username
            or url.password
            or url.path
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "public_origin must be an HTTPS origin (HTTP loopback for development)"
            )
        if not self.database_url.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("PostgreSQL required")
        if self.eval_database_url and not self.eval_database_url.get_secret_value().startswith(
            "postgresql+psycopg://"
        ):
            raise ValueError("Evaluation archive requires PostgreSQL")
        return self


class Sessions:
    def __init__(self, password, idle=900, clock=time.monotonic):
        if len(password) < 32:
            raise ValueError("Dashboard requires an independent random 32+ character password")
        self.password, self.idle, self.clock = password, idle, clock
        self.sessions, self.attempts = {}, deque()
        self.lock = threading.Lock()

    def login(self, password, previous=None):
        with self.lock:
            now = self.clock()
            while self.attempts and self.attempts[0] <= now - 60:
                self.attempts.popleft()
            if len(self.attempts) >= 10:
                raise VeyError("rate_limited", "登录尝试过多，请一分钟后重试", 429)
            self.attempts.append(now)
            if not secrets.compare_digest(password.encode(), self.password.encode()):
                raise VeyError("unauthorized", "登录口令不正确", 401)
            self.sessions = {k: v for k, v in self.sessions.items() if v[0] > now and v[1] > now}
            self.sessions.pop(previous, None)
            if len(self.sessions) >= 8:
                del self.sessions[next(iter(self.sessions))]
            token = secrets.token_urlsafe(32)
            self.sessions[token] = (now + self.idle, now + 8 * 3600)
            return token

    def require(self, token):
        with self.lock:
            now = self.clock()
            idle, absolute = self.sessions.get(token, (0, 0))
            if min(idle, absolute) <= now:
                self.sessions.pop(token, None)
                raise VeyError("unauthorized", "请登录，或重新登录已过期的会话", 401)
            self.sessions[token] = (now + self.idle, absolute)

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)


def display_value(value):
    """Extra defensive filtering; ordinary error codes remain useful."""
    value = safe_value(value)
    if isinstance(value, dict):
        return {
            k: display_value(v)
            for k, v in value.items()
            if k not in {"cursor", "generation", "text", "history", "user_id", "message_id"}
        }
    if isinstance(value, list):
        return [display_value(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"\b[A-Fa-f0-9]{8}\b", "[隐藏确认码]", value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


class DashboardStore:
    def __init__(self, factory):
        self.factory = factory

    def overview(self):
        with self.factory() as db:
            rows = (
                db.execute(
                    text(
                        "SELECT status, count(*) AS count FROM vey_dashboard.tasks GROUP BY status"
                    )
                )
                .mappings()
                .all()
            )
        return {"counts": {r["status"]: r["count"] for r in rows}, "scope": "retained_tasks"}

    def tasks(self, status=None, before=None, limit=20):
        with self.factory() as db:
            condition, params = [], {"limit": limit + 1}
            if status:
                condition.append("status=:status")
                params["status"] = status
            if before:
                anchor = (
                    db.execute(
                        text("SELECT created_at,id FROM vey_dashboard.tasks WHERE id=:id"),
                        {"id": before},
                    )
                    .mappings()
                    .first()
                )
                if anchor is None:
                    raise VeyError("expired_cursor", "分页位置已清理，请刷新列表", 409)
                condition.append("(created_at,id)<(:created_at,:id)")
                params.update(anchor)
            where = " WHERE " + " AND ".join(condition) if condition else ""
            rows = (
                db.execute(
                    text(
                        "SELECT * FROM vey_dashboard.tasks"
                        + where
                        + " ORDER BY created_at DESC,id DESC LIMIT :limit"
                    ),
                    params,
                )
                .mappings()
                .all()
            )
        return {
            "items": [display_value(dict(r)) for r in rows[:limit]],
            "next": rows[limit - 1]["id"] if len(rows) > limit else None,
        }

    def detail(self, task_id):
        with self.factory() as db:
            task = (
                db.execute(text("SELECT * FROM vey_dashboard.tasks WHERE id=:id"), {"id": task_id})
                .mappings()
                .first()
            )
            if task is None:
                raise VeyError("not_found", "任务不存在或已超过审计保留期", 404)
            events = (
                db.execute(
                    text(
                        "SELECT id,kind,data,created_at FROM vey_dashboard.events "
                        "WHERE task_id=:id ORDER BY id LIMIT 101"
                    ),
                    {"id": task_id},
                )
                .mappings()
                .all()
            )
            operation = (
                db.execute(
                    text("SELECT * FROM vey_dashboard.operations WHERE task_id=:id"),
                    {"id": task_id},
                )
                .mappings()
                .first()
            )
        return display_value(
            {
                "task": dict(task),
                "events": [dict(e) for e in events[:100]],
                "events_truncated": len(events) > 100,
                "operation": dict(operation) if operation else None,
            }
        )


def create_app(settings=None, store=None, sessions=None, evaluation=None, configuration=None):
    settings = settings or DashboardSettings()
    sessions = sessions or Sessions(
        settings.password_file.read_text().strip(), settings.session_seconds
    )
    engine = None
    if store is None:
        engine, factory = database(settings.database_url.get_secret_value())
        store = DashboardStore(factory)
    if evaluation is None and settings.eval_database_url:
        evaluation = EvaluationReader(settings.eval_database_url.get_secret_value())
    if configuration is None and settings.configuration_token_file:
        configuration = ConfigurationClient(
            settings.configuration_socket, settings.configuration_token_file.read_text().strip()
        )

    @asynccontextmanager
    async def lifespan(app):
        yield
        if engine:
            engine.dispose()
        if configuration:
            configuration.close()

    app = FastAPI(
        title="Vey 控制台", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    install_errors(app)

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
            }
        )
        return response

    def auth(request: Request):
        sessions.require(request.cookies.get(COOKIE))

    def origin(request: Request):
        if request.headers.get("origin") != settings.public_origin:
            raise VeyError("forbidden_origin", "请求来源不匹配", 403)

    @app.get("/health/ready")
    def ready():
        store.overview()
        return {"status": "ready"}

    @app.get("/admin/")
    def index():
        return FileResponse(ASSETS / "index.html")

    @app.get("/admin/assets/{name}")
    def asset(name: str):
        if name not in {"app.js", "style.css", "evidence.js", "configuration.js"}:
            raise VeyError("not_found", "文件不存在", 404)
        return FileResponse(ASSETS / name)

    @app.post("/admin/api/login", dependencies=[Depends(origin)])
    async def login(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1024:
                raise VeyError("oversized_message", "登录请求过大", 413)
        try:
            data = json.loads(body)
            password = data["password"]
            if not isinstance(password, str) or len(password) > 256:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise VeyError("invalid_login", "请输入登录口令", 400) from None
        token = sessions.login(password, request.cookies.get(COOKIE))
        response = JSONResponse({"authenticated": True})
        response.set_cookie(COOKIE, token, secure=True, httponly=True, samesite="strict", path="/")
        return response

    @app.post("/admin/api/logout", dependencies=[Depends(origin)])
    def logout(request: Request):
        sessions.logout(request.cookies.get(COOKIE))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/admin/api/overview", dependencies=[Depends(auth)])
    def overview():
        return store.overview()

    @app.get("/admin/api/operations", dependencies=[Depends(auth)])
    def operations():
        return operations_status(settings.operations_report_dir)

    def configuration_service():
        if configuration is None:
            raise VeyError("configuration_not_enabled", "配置管理尚未启用", 503)
        return configuration

    @app.get("/admin/api/configuration", dependencies=[Depends(auth)])
    def configuration_view():
        return configuration_service().call()

    async def change_request(request, action):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise VeyError("oversized_configuration", "配置请求最多 64 KiB", 413)
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, TypeError):
            raise VeyError("invalid_configuration", "配置请求格式不正确") from None
        return await asyncio.to_thread(configuration_service().call, action, data)

    @app.post("/admin/api/configuration/preview", dependencies=[Depends(auth), Depends(origin)])
    async def configuration_preview(request: Request):
        return await change_request(request, "/preview")

    @app.post("/admin/api/configuration/publish", dependencies=[Depends(auth), Depends(origin)])
    async def configuration_publish(request: Request):
        return await change_request(request, "/publish")

    @app.get("/admin/api/tasks", dependencies=[Depends(auth)])
    def tasks(
        status: str | None = None,
        before: str | None = Query(default=None, pattern=r"^[a-f0-9]{32}$"),
        limit: int = Query(default=20, ge=1, le=50),
    ):
        if status and status not in STATUSES:
            raise VeyError("invalid_status", "不支持的任务状态")
        return store.tasks(status, before, limit)

    @app.get("/admin/api/tasks/{task_id}", dependencies=[Depends(auth)])
    def detail(task_id: str):
        if not re.fullmatch(r"[a-f0-9]{32}", task_id):
            raise VeyError("not_found", "任务编号格式不正确", 404)
        return store.detail(task_id)

    @app.get("/admin/api/tasks/{task_id}/export", dependencies=[Depends(auth)])
    def export(task_id: str):
        if not re.fullmatch(r"[a-f0-9]{32}", task_id):
            raise VeyError("not_found", "任务编号格式不正确", 404)
        try:
            payload = export_snapshot(store.detail(task_id))
        except ValueError:
            raise VeyError(
                "export_too_large",
                "审计导出超过大小限制（总计 1 MB／单字段 12000 字符），未生成文件",
                413,
            ) from None
        return Response(
            payload,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="vey-audit-{task_id}.json"'},
        )

    def require_evaluation():
        if evaluation is None:
            raise VeyError("evaluation_not_configured", "尚未配置独立评测库只读连接", 503)
        return evaluation

    @app.get("/admin/api/evaluations", dependencies=[Depends(auth)])
    def evaluations(
        before: str | None = Query(default=None, pattern=r"^[a-f0-9]{32}$"),
        limit: int = Query(default=20, ge=1, le=30),
    ):
        return require_evaluation().runs(before, limit)

    @app.get("/admin/api/evaluations/{run_id}", dependencies=[Depends(auth)])
    def evaluation_detail(
        run_id: str, offset: int = Query(default=0, ge=0, le=10000), status: str | None = None
    ):
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise VeyError("not_found", "评测编号格式不正确", 404)
        if status and status not in {"passed", "failed", "not_run", "uncovered"}:
            raise VeyError("invalid_status", "不支持的样本状态")
        return require_evaluation().detail(run_id, offset, status)

    return app
