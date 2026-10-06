import json
import uuid
from datetime import timedelta

import httpx
import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from vey.dashboard import (
    COOKIE,
    DashboardSettings,
    DashboardStore,
    Sessions,
    create_app,
    display_value,
)
from vey.dashboard_db import install_views
from vey.db import Event, Grant, Task, database, uid
from vey.domain import VeyError, utcnow


@pytest.fixture
def dashboard_settings(tmp_path):
    key = tmp_path / "dashboard_password"
    key.write_text("independent-dashboard-credential-" + "a" * 32)
    return DashboardSettings(
        database_url="postgresql+psycopg://x@localhost/vey_test",
        password_file=key,
        public_origin="https://ops.example.com",
    )


def test_sessions_rotation_expiry_logout_and_rate_limit():
    now = [1000]
    sessions = Sessions("a" * 40, idle=60, clock=lambda: now[0])
    first = sessions.login("a" * 40)
    second = sessions.login("a" * 40, first)
    with pytest.raises(VeyError):
        sessions.require(first)
    sessions.require(second)
    now[0] += 61
    with pytest.raises(VeyError):
        sessions.require(second)
    third = sessions.login("a" * 40)
    sessions.logout(third)
    with pytest.raises(VeyError):
        sessions.require(third)
    for _ in range(9):
        with pytest.raises(VeyError, match="不正确"):
            sessions.login("错误口令")
    with pytest.raises(VeyError) as error:
        sessions.login("a" * 40)
    assert error.value.status == 429
    now[0] += 61
    sessions.require(sessions.login("a" * 40))


def test_sessions_have_absolute_lifetime_and_bounded_storage():
    now = [1]
    sessions = Sessions("a" * 40, idle=3600, clock=lambda: now[0])
    token = sessions.login("a" * 40)
    for _ in range(8):
        now[0] += 3500
        sessions.require(token)
    now[0] += 801
    with pytest.raises(VeyError):
        sessions.require(token)
    for _ in range(10):
        sessions.login("a" * 40)
    assert len(sessions.sessions) == 8


class FakeStore:
    def overview(self):
        return {"counts": {"succeeded": 2}, "scope": "retained_tasks"}

    def tasks(self, status, before, limit):
        return {"items": [], "next": None}

    def detail(self, task_id):
        return {"task": {"id": task_id}}


async def test_http_auth_csrf_cookie_headers_and_read_only_surface(dashboard_settings):
    app = create_app(dashboard_settings, FakeStore())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=dashboard_settings.public_origin
    ) as client:
        response = await client.get("/admin/")
        assert response.status_code == 200
        assert "每次执行" in response.text
        assert response.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        for endpoint in ("overview", "tasks", "tasks/" + "a" * 32):
            assert (await client.get("/admin/api/" + endpoint)).status_code == 401
        password = dashboard_settings.password_file.read_text()
        assert (
            await client.post("/admin/api/login", json={"password": password})
        ).status_code == 403
        headers = {"Origin": dashboard_settings.public_origin}
        assert (
            await client.post(
                "/admin/api/login",
                headers={"Origin": "https://evil.test"},
                json={"password": password},
            )
        ).status_code == 403
        response = await client.post(
            "/admin/api/login", headers=headers, json={"password": password}
        )
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert all(
            part in cookie for part in (COOKIE, "HttpOnly", "Secure", "SameSite=strict", "Path=/")
        )
        assert password not in response.text
        assert (await client.get("/admin/api/overview")).json()["counts"] == {"succeeded": 2}
        assert (await client.get("/admin/api/tasks?limit=51")).status_code == 422
        assert (await client.get("/admin/api/tasks?status=sql-injection")).status_code == 400
        assert (await client.get("/admin/api/tasks?before='OR1=1")).status_code == 422
        assert (await client.get("/admin/api/tasks/not-a-task-id")).status_code == 404
        assert (await client.post("/admin/api/tasks", json={})).status_code == 405
        assert (await client.post("/admin/api/confirm", json={})).status_code == 404
        assert (await client.get("/admin/assets/unknown.js")).status_code == 404
        assert (await client.post("/admin/api/logout")).status_code == 403
        old_cookie = client.cookies.get(COOKIE)
        assert (await client.post("/admin/api/logout", headers=headers)).status_code == 200
        client.cookies.set(COOKIE, old_cookie)
        assert (await client.get("/admin/api/tasks")).status_code == 401


async def test_login_rejects_oversized_invalid_and_non_string_input(dashboard_settings):
    app = create_app(dashboard_settings, FakeStore())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=dashboard_settings.public_origin
    ) as client:
        headers = {"Origin": dashboard_settings.public_origin}
        for body in ("[]", '{"password":123}', "null", "not JSON"):
            assert (
                await client.post("/admin/api/login", headers=headers, content=body)
            ).status_code == 400
        assert (
            await client.post("/admin/api/login", headers=headers, content="a" * 1025)
        ).status_code == 413


def test_presentation_hides_confirmation_and_session_material():
    value = display_value(
        {
            "result": "确认 ABCD1234 password=do-not-show",
            "cursor": "secretcursor",
            "user_id": "private-owner",
            "data": {"code": "ABCD1234", "text": "raw log"},
        }
    )
    data = json.dumps(value)
    assert all(
        secret not in data
        for secret in ("ABCD1234", "do-not-show", "secretcursor", "private-owner", "raw log")
    )


@pytest.fixture
def dashboard_pg(db_factory, migrated_database):
    engine, _ = migrated_database
    url = engine.url.render_as_string(hide_password=False)
    native = url.replace("postgresql+psycopg://", "postgresql://")
    role = "vey_test_dashboard_" + uuid.uuid4().hex[:12]
    password = uuid.uuid4().hex + uuid.uuid4().hex
    with psycopg.connect(native, autocommit=True) as admin:
        admin.execute("DROP SCHEMA IF EXISTS vey_dashboard CASCADE")
        with admin.transaction():
            install_views(admin, password, role)
        reader_url = make_url(url).set(username=role, password=password)
        reader_engine, factory = database(reader_url.render_as_string(hide_password=False))
        try:
            yield DashboardStore(factory), reader_url.render_as_string(hide_password=False)
        finally:
            reader_engine.dispose()
            admin.execute("DROP SCHEMA vey_dashboard CASCADE")
            admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


@pytest.mark.postgres
def test_views_role_cannot_read_raw_data_or_write_even_with_readonly_disabled(dashboard_pg):
    _, url = dashboard_pg
    with psycopg.connect(
        url.replace("postgresql+psycopg://", "postgresql://"), autocommit=True
    ) as db:
        assert db.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
        db.execute("SET default_transaction_read_only=off")
        for query in (
            "SELECT * FROM vey_core.sessions",
            "SELECT * FROM vey_core.tasks",
            "SELECT * FROM vey_exec.grants",
            "SELECT * FROM vey_core.outbox",
            "DELETE FROM vey_dashboard.tasks",
            "UPDATE vey_dashboard.operations SET status='succeeded'",
            "CREATE TABLE vey_dashboard.bypass(id int)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db.execute(query)
        assert db.execute("SELECT count(*) FROM vey_dashboard.tasks").fetchone()[0] == 0


@pytest.mark.postgres
def test_real_query_pagination_detail_and_absent_sensitive_fields(dashboard_pg, db_factory):
    store, url = dashboard_pg
    now = utcnow()
    ids = sorted([uid() for _ in range(3)], reverse=True)
    with db_factory.begin() as db:
        for i, task_id in enumerate(ids):
            db.add(
                Task(
                    id=task_id,
                    message_id=uid(),
                    user_id="private-owner",
                    generation=uid(),
                    text="private original conversation",
                    status="succeeded" if i else "awaiting_confirmation",
                    result="确认 ABCD1234",
                    created_at=now,
                    updated_at=now,
                )
            )
        db.flush()
        db.add(
            Event(
                task_id=ids[0],
                kind="operation_prepared",
                data={"code": "ABCD1234", "target": "demo/web"},
            )
        )
        db.add(
            Event(
                task_id=ids[0],
                kind="tool",
                data={
                    "call": {"name": "logs"},
                    "result": {"log_excerpt": "<script>alert(1)</script> password=hidden"},
                },
            )
        )
        db.add(
            Grant(
                code="ABCD1234",
                task_id=ids[0],
                user_id="private-owner",
                generation=uid(),
                target="demo/web",
                action="restart",
                container_ids=["container"],
                policy_hash="a" * 64,
                expires_at=now + timedelta(minutes=2),
            )
        )
    first = store.tasks(limit=2)
    assert [r["id"] for r in first["items"]] == ids[:2]
    assert first["next"] == ids[1]
    assert [r["id"] for r in store.tasks(before=first["next"])["items"]] == ids[2:]
    assert len(store.tasks(status="succeeded")["items"]) == 2
    assert store.overview()["counts"] == {"succeeded": 2, "awaiting_confirmation": 1}
    detail = store.detail(ids[0])
    assert detail["operation"]["status"] == "pending"
    assert "<script>" in str(detail)  # Browser renders this with textContent, not HTML.
    assert len(detail["events"]) == 2
    assert "ABCD1234" not in json.dumps(detail)
    assert "private-owner" not in json.dumps(detail)
    assert "original conversation" not in json.dumps(detail)
    with pytest.raises(VeyError) as error:
        store.tasks(before="f" * 32)
    assert error.value.status == 409
    with pytest.raises(VeyError):
        store.detail("f" * 32)
    with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://")) as db:
        assert (
            "ABCD1234"
            not in db.execute("SELECT result FROM vey_dashboard.tasks LIMIT 1").fetchone()[0]
        )
        assert (
            "code"
            not in db.execute(
                "SELECT data FROM vey_dashboard.events WHERE kind='operation_prepared'"
            ).fetchone()[0]
        )
