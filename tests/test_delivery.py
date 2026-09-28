import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from vey.core import Core
from vey.db import Outbox, Task
from vey.domain import utcnow
from vey.wecom import delivery_parts

pytestmark = pytest.mark.postgres


def queue_result(core, body="测试结果"):
    task = core.accept("delivery-test", "owner", "服务列表")
    with core.factory.begin() as db:
        row = Outbox(task_id=task["task_id"], user_id="owner", body=body)
        db.add(row)
        db.flush()
        return row.id, task["task_id"]


class Sender:
    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = fail_on

    async def send(self, user, body):
        self.calls.append((user, body))
        if len(self.calls) in self.fail_on:
            raise TimeoutError()


async def test_long_result_resumes_after_failure_and_core_recreation(core, backend):
    # Identical log segments must still have distinct payloads for provider deduplication.
    body = "中🙂" * 1500
    row_id, task_id = queue_result(core, body)
    expected = delivery_parts(body, task_id, "result")
    assert len(expected) > 2
    assert len(set(expected)) == len(expected)
    assert all(len(part.encode()) <= 1800 for part in expected)
    assert "".join(part.split("\n", 1)[1] for part in expected) == body
    sender = Sender(fail_on={2})
    await core.send_pending(sender)
    await core.send_pending(sender)
    with core.factory.begin() as db:
        row = db.get(Outbox, row_id)
        assert row.sent_parts == 1 and row.attempts == 1 and row.status == "pending"
        row.next_at = utcnow() - timedelta(seconds=1)
    restarted = Core(core.factory, core.policy, core.executor, core.model)
    while await restarted.send_pending(sender):
        pass
    assert [body for _, body in sender.calls] == [expected[0], expected[1], *expected[1:]]
    with core.factory() as db:
        row = db.get(Outbox, row_id)
        assert row.status == "sent" and row.sent_parts == len(expected)
    assert not backend.actions


async def test_failed_part_stops_after_ten_attempts(core):
    row_id, _ = queue_result(core)
    sender = Sender(fail_on=set(range(1, 20)))
    for _ in range(10):
        with core.factory.begin() as db:
            db.get(Outbox, row_id).next_at = utcnow() - timedelta(seconds=1)
        assert await core.send_pending(sender)
    assert not await core.send_pending(sender)
    with core.factory() as db:
        row = db.get(Outbox, row_id)
        assert row.status == "failed" and row.sent_parts == 0 and row.attempts == 10
    assert len(sender.calls) == 10


async def test_crash_during_final_attempt_becomes_failed_without_resend(core):
    row_id, _ = queue_result(core)
    with core.factory.begin() as db:
        row = db.get(Outbox, row_id)
        row.status, row.attempts = "sending", 10
        row.next_at = utcnow() - timedelta(seconds=1)
    sender = Sender()
    assert await core.send_pending(sender)
    assert not sender.calls
    with core.factory() as db:
        assert db.get(Outbox, row_id).status == "failed"


async def test_delivery_lease_prevents_immediate_resend_after_crash(core):
    row_id, _ = queue_result(core)
    with core.factory.begin() as db:
        row = db.get(Outbox, row_id)
        row.status, row.attempts = "sending", 1
        row.next_at = utcnow() + timedelta(seconds=60)
    sender = Sender()
    assert not await core.send_pending(sender)
    with core.factory.begin() as db:
        db.get(Outbox, row_id).next_at = utcnow() - timedelta(seconds=1)
    assert await core.send_pending(sender)
    assert len(sender.calls) == 1


async def test_expired_message_is_not_sent_even_before_cleanup(core):
    row_id, _ = queue_result(core)
    with core.factory.begin() as db:
        db.get(Outbox, row_id).created_at = utcnow() - timedelta(hours=2)
    sender = Sender()
    await core.send_pending(sender)
    assert not sender.calls
    with core.factory() as db:
        row = db.get(Outbox, row_id)
        assert row.status == "failed" and row.body == ""


async def test_late_ack_does_not_resurrect_expired_delivery(core):
    row_id, _ = queue_result(core)
    started, release = asyncio.Event(), asyncio.Event()

    class PausedSender:
        async def send(self, *args):
            started.set()
            await release.wait()

    delivery = asyncio.create_task(core.send_pending(PausedSender()))
    await asyncio.wait_for(started.wait(), timeout=2)
    try:
        with core.factory.begin() as db:
            db.get(Outbox, row_id).created_at = utcnow() - timedelta(hours=2)
        core.cleanup()
    finally:
        release.set()
        await delivery
    with core.factory() as db:
        row = db.get(Outbox, row_id)
        assert row.status == "failed" and row.body == "" and row.sent_parts == 0


def test_recovery_notifies_once_without_replaying_running_task(core, backend):
    task = core.accept("interrupted", "owner", "重启 博客", "wecom")
    core.claim()
    core.recover()
    core.recover()
    assert core.claim() is None
    with core.factory() as db:
        assert db.get(Task, task["task_id"]).status == "unknown"
        rows = list(db.scalars(select(Outbox).where(Outbox.kind == "result")))
        assert len(rows) == 1
        assert task["task_id"] in rows[0].body and "未自动重放" in rows[0].body
    assert not backend.actions


async def test_repeated_callback_after_completion_keeps_one_task_and_result(core):
    first = core.accept("same-wecom-msg", "owner", "服务列表", "wecom")
    await core.process(core.claim())
    duplicate = core.accept("same-wecom-msg", "owner", "服务列表", "wecom")
    assert duplicate["duplicate"] and duplicate["task_id"] == first["task_id"]
    assert core.claim() is None
    with core.factory() as db:
        assert db.scalar(select(func.count()).select_from(Task)) == 1
        assert db.scalar(select(func.count()).select_from(Outbox)) == 2
