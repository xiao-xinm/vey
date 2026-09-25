import asyncio
import os
import runpy
from datetime import timedelta

import psycopg
import pytest
from sqlalchemy import select
from test_postgres import prepare

from vey.db import Grant, Outbox
from vey.domain import ConfirmRequest, Intent, NextStep, ToolCall, VeyError, utcnow

pytestmark = pytest.mark.postgres


def test_runtime_roles_cannot_cross_schemas_or_create_tables(db_factory):
    configure = runpy.run_path("scripts/bootstrap_database.py")["grant_runtime_access"]
    url = os.environ["VEY_TEST_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as db, db.transaction(force_rollback=True):
        configure(db, {"vey_core": "c" * 32, "vey_executor": "e" * 32})
        for role, own, other in (
            ("vey_core", "vey_core.tasks", "vey_exec.grants"),
            ("vey_executor", "vey_exec.grants", "vey_core.tasks"),
        ):
            db.execute(f"SET LOCAL ROLE {role}")
            db.execute(f"SELECT * FROM {own} LIMIT 0")
            for statement in (
                f"SELECT * FROM {other} LIMIT 0",
                "CREATE TABLE public.should_not_exist (id int)",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
                    db.execute(statement)
            db.execute("RESET ROLE")


async def test_time_budget_summarizes_existing_evidence(core, monkeypatch):
    class SlowModel:
        async def route(self, *args):
            return Intent(kind="diagnose", target="blog/web")

        async def next_step(self, message, evidence, *args):
            if evidence:
                await asyncio.sleep(10)
            return NextStep(tool=ToolCall(name="inspect", target="blog/web"))

    monkeypatch.setattr("vey.core.CHECK_TIMEOUT_SECONDS", 0.2)
    core.model = SlowModel()
    task = core.accept("time-budget", "owner", "排查博客")
    await core.process(core.claim())
    result = core.task(task["task_id"])
    assert result["status"] == "partial" and "[E1]" in result["result"]


async def test_outbox_retries_do_not_repeat_docker(core, backend):
    class Sender:
        def __init__(self):
            self.calls = 0

        async def send(self, *args):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError()

    task = core.accept("outbox", "owner", "服务列表", "wecom")
    await core.process(core.claim())
    sender = Sender()
    await core.send_pending(sender)
    with core.factory.begin() as db:
        rows = list(db.scalars(select(Outbox).where(Outbox.task_id == task["task_id"])))
        assert len(rows) == 2
        for row in rows:
            row.next_at = utcnow() - timedelta(seconds=1)
    await core.send_pending(sender)
    await core.send_pending(sender)
    assert not backend.actions
    with core.factory() as db:
        assert all(item.status == "sent" for item in db.scalars(select(Outbox)))


def test_unknown_operation_reconciliation_keeps_evidence(executor, actor, backend):
    backend.fail = True
    grant = prepare(executor, actor)
    executor.confirm(ConfirmRequest(actor=actor, code=grant["code"]))
    reconcile = runpy.run_path("scripts/reconcile_operation.py")["reconcile"]
    result = reconcile(
        executor.factory, grant["task_id"], "已在服务器核对容器状态，接受未知历史结果并解除锁定"
    )
    assert result["status"] == "reconciled" and len(backend.actions) == 1
    with executor.factory() as db:
        saved = db.get(Grant, grant["code"])
        assert saved.result["reconciliation"]["previous_status"] == "unknown"
        assert saved.result["instances"][0]["operation"] == "unknown"


def test_container_running_is_not_business_recovered(executor, actor, backend, monkeypatch):
    monkeypatch.setattr(backend, "health", lambda service: {"status": "failed", "http_status": 503})
    grant = prepare(executor, actor)
    result = executor.confirm(ConfirmRequest(actor=actor, code=grant["code"]))
    assert result["status"] == "partial"
    assert result["result"]["business_health"]["status"] == "failed"


async def test_confirmation_response_loss_marks_original_unknown(core, backend, monkeypatch):
    task = core.accept("response-loss", "owner", "重启 博客")
    await core.process(core.claim())
    with core.factory() as db:
        code = db.scalar(select(Grant.code).where(Grant.task_id == task["task_id"]))

    async def lost_response(request):
        core.executor.service.confirm(request)
        raise VeyError("executor_unavailable", "response lost", 503)

    monkeypatch.setattr(core.executor, "confirm", lost_response)
    control = core.accept("confirm-lost", "owner", "确认 " + code)
    await core.process(core.claim(control=True))
    assert core.task(task["task_id"])["status"] == "unknown"
    assert core.task(control["task_id"])["status"] == "unknown"
    assert len(backend.actions) == 1
