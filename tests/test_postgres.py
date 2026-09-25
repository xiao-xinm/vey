from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select

from vey.db import ChatSession, Event, Grant, Task, uid
from vey.domain import (
    Action,
    ConfirmRequest,
    PrepareRequest,
    ReadRequest,
    ToolCall,
    VeyError,
    utcnow,
)

pytestmark = pytest.mark.postgres


def prepare(executor, actor, target="blog/web", action=Action.RESTART):
    return executor.prepare(
        PrepareRequest(actor=actor, task_id=uid(), target=target, action=action)
    )


def test_concurrent_confirmation_executes_once(executor, actor, backend):
    grant = prepare(executor, actor)
    request = ConfirmRequest(actor=actor, code=grant["code"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: executor.confirm(request), range(2)))
    assert len(backend.actions) == 1
    assert all(r["status"] in {"executing", "succeeded"} for r in results)
    assert executor.confirm(request)["status"] == "succeeded"
    assert len(backend.actions) == 1


@pytest.mark.parametrize("target", ["数据库", "infra/postgres", "自己", "vey/agent-core"])
def test_protected_service_cannot_even_prepare(executor, actor, target, backend):
    with pytest.raises(VeyError, match="受保护"):
        prepare(executor, actor, target)
    assert backend.actions == []


def test_expiry_and_target_change_rejected(executor, actor, db_factory, backend):
    grant = prepare(executor, actor)
    with db_factory.begin() as db:
        db.get(Grant, grant["code"]).expires_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(VeyError, match="2 分钟"):
        executor.confirm(ConfirmRequest(actor=actor, code=grant["code"]))
    next_grant = prepare(executor, actor)
    backend.instances["blog/web"][0]["id"] = "z" * 64
    with pytest.raises(VeyError, match="变化"):
        executor.confirm(ConfirmRequest(actor=actor, code=next_grant["code"]))
    assert not backend.actions


def test_timeout_never_replays_and_blocks_new_modify(executor, actor, backend):
    backend.fail = True
    grant = prepare(executor, actor)
    request = ConfirmRequest(actor=actor, code=grant["code"])
    assert executor.confirm(request)["status"] == "unknown"
    assert executor.confirm(request)["status"] == "unknown"
    next_grant = prepare(executor, actor)
    with pytest.raises(VeyError, match="结果未知"):
        executor.confirm(ConfirmRequest(actor=actor, code=next_grant["code"]))
    assert len(backend.actions) == 1


def test_clearing_session_revokes_grants_and_cursors(executor, actor):
    grant = prepare(executor, actor)
    first = executor.read(ReadRequest(actor=actor, call=ToolCall(name="logs", target="blog/web")))
    assert first["cursor"]
    new_actor = actor.model_copy(update={"generation": "new-session", "started_at": utcnow()})
    executor.sync_actor(new_actor)
    with pytest.raises(VeyError):
        executor.confirm(ConfirmRequest(actor=new_actor, code=grant["code"]))
    with pytest.raises(VeyError):
        executor.read(
            ReadRequest(actor=new_actor, call=ToolCall(name="logs", cursor=first["cursor"]))
        )
    with pytest.raises(VeyError):
        executor.sync_actor(actor)


def test_log_cursor_pages_and_hard_character_limit(executor, actor, backend):
    first = executor.read(ReadRequest(actor=actor, call=ToolCall(name="logs", target="blog/web")))
    second = executor.read(
        ReadRequest(actor=actor, call=ToolCall(name="logs", cursor=first["cursor"]))
    )
    assert first["text"].splitlines() == backend.records[-100:]
    assert second["text"].splitlines() == backend.records[-200:-100]
    backend.records = ["older", "中" * 25000]
    first = executor.read(
        ReadRequest(actor=actor, call=ToolCall(name="logs", target="blog/web", lines=1))
    )
    second = executor.read(
        ReadRequest(actor=actor, call=ToolCall(name="logs", cursor=first["cursor"], lines=1))
    )
    assert first["truncated"] and len(first["text"]) == 10000
    assert len(second["text"]) == 10000 and "older" not in second["text"]


async def test_message_dedup_and_confirmation_end_to_end(core, backend):
    first = core.accept("msg-1", "owner", "重启 博客")
    duplicate = core.accept("msg-1", "owner", "重启 博客")
    assert first["task_id"] == duplicate["task_id"] and duplicate["duplicate"]
    task_id = core.claim()
    await core.process(task_id)
    assert core.task(task_id)["status"] == "awaiting_confirmation"
    assert not backend.actions
    with core.factory() as db:
        code = db.scalar(select(Grant.code).where(Grant.task_id == task_id))
    control = core.accept("msg-2", "owner", "确认 " + code)
    await core.process(core.claim(control=True))
    assert core.task(control["task_id"])["status"] == "succeeded"
    assert core.task(task_id)["status"] == "succeeded"
    assert len(backend.actions) == 1


async def test_diagnosis_stops_at_five_calls(core):
    task = core.accept("diagnose", "owner", "为什么访问不了")
    await core.process(core.claim())
    result = core.task(task["task_id"])
    assert "5 次" in result["result"]
    with core.factory() as db:
        events = list(
            db.scalars(select(Event).where(Event.task_id == task["task_id"], Event.kind == "tool"))
        )
        assert len(events) == 5


async def test_expired_context_does_not_reappear(core):
    task = core.accept("old", "owner", "服务器概况")
    with core.factory.begin() as db:
        db.get(ChatSession, "owner").expires_at = utcnow() - timedelta(seconds=1)
    new = core.accept("new", "owner", "服务列表")
    await core.process(task["task_id"])
    assert core.task(task["task_id"])["status"] == "cancelled"
    with core.factory() as db:
        assert db.get(Task, task["task_id"]).text == ""
        assert db.get(Task, new["task_id"]).text == "服务列表"


def test_audit_cleanup_30_days_and_clear_independence(core):
    item = core.accept("audit", "owner", "服务器概况")
    with core.factory.begin() as db:
        task = db.get(Task, item["task_id"])
        task.status = "succeeded"
        task.created_at = utcnow() - timedelta(days=29)
    core.accept("clear", "owner", "清空上下文")
    core.cleanup()
    assert core.task(item["task_id"])
    with core.factory.begin() as db:
        db.get(Task, item["task_id"]).created_at = utcnow() - timedelta(days=31)
    core.cleanup()
    with pytest.raises(VeyError):
        core.task(item["task_id"])


def test_unauthorized_identity_never_gets_task(core):
    with pytest.raises(VeyError):
        core.accept("attacker", "someone-else", "重启 博客")
