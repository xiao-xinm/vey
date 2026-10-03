from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from vey.config import Thresholds
from vey.db import ChatSession, Outbox
from vey.domain import Intent, ReadRequest, ToolCall, utcnow
from vey.presentation import render_logs, render_operation, render_stats, render_system
from vey.system_info import collect_system, system_alerts


def test_log_summary_is_bounded_evidence_not_diagnosis():
    page = {
        "target": "blog/web",
        "text": "info hello\nERROR password=private\nWARNING slow\n" + "中" * 9000,
        "message": "日志已截断（10000 字符上限），可继续翻页",
    }
    result = render_logs(page)
    assert "错误类关键词 1 行" in result and "警告类 1 行" in result
    assert "不代表已确认故障" in result and "展开日志" in result
    assert "private" not in result and len(result) < 1800
    assert "已截断" in result
    assert "没有匹配日志" in render_logs({"text": ""})


def test_thresholds_missing_data_and_mounts(tmp_path, monkeypatch):
    monkeypatch.setattr("vey.system_info.platform.system", lambda: "Linux")
    monkeypatch.setattr("time.sleep", lambda _: None)
    (tmp_path / "meminfo").write_text("MemTotal: 100 kB\nMemAvailable: 10 kB\n")
    (tmp_path / "stat").write_text("cpu 1 1 1 1 1 0 0 0\ncpu0 1 1 1 1 1 0 0 0\n")
    (tmp_path / "loadavg").write_text("3.0 1 1 0/1 1")
    (tmp_path / "1").mkdir()
    (tmp_path / "1/mountinfo").write_text(
        "1 0 8:1 / / rw - ext4 /dev/vda1 rw\n2 0 8:2 / /data rw - xfs /dev/vdb rw\n"
    )
    monkeypatch.setattr(
        "os.statvfs",
        lambda _: SimpleNamespace(f_blocks=100, f_bavail=2, f_bfree=4, f_frsize=1024),
        raising=False,
    )
    result = collect_system(tmp_path, ["/mapped"], {"/mapped": "/"}, Thresholds())
    assert result["unmapped_mounts"] == ["/data"]
    assert len(result["alerts"]) == 3
    assert "未配置容量采集：/data" in render_system(result)
    assert system_alerts({"memory": None, "cpu_percent": None}, Thresholds()) == []
    with pytest.raises(ValidationError):
        Thresholds(cpu_percent=101)
    # Loss of CPU counters must not hide independently available memory data.
    (tmp_path / "stat").unlink()
    assert collect_system(tmp_path, [])["memory"]["MemTotal"] == 102400


def test_operation_partial_and_unknown_are_not_success():
    result = {
        "target": "blog/web",
        "action": "restart",
        "status": "partial",
        "task_id": "id",
        "result": {
            "business_health": {"status": "failed"},
            "instances": [
                {
                    "id": "a",
                    "operation": "completed",
                    "expected_state_observed": True,
                    "verification": {"status": "running"},
                }
            ],
        },
    }
    rendered = render_operation(result)
    assert "部分完成" in rendered and "业务健康检查失败" in rendered
    result["status"] = "unknown"
    result["result"]["instances"] = [{"id": "a", "operation": "unknown"}]
    assert "未自动重试" in render_operation(result)
    assert "暂不可用" in render_stats({"instances": [{"id": "a", "cpu_percent": None}]})


@pytest.mark.postgres
def test_resource_ranking_orders_only_configured_instances(executor, actor, backend, monkeypatch):
    monkeypatch.setattr(
        backend,
        "stats",
        lambda cid: {
            "id": cid,
            "memory_bytes": {"a": 100, "b": 300, "c": 200}[cid[0]],
            "cpu_percent": {"a": 50, "b": 1, "c": 0}[cid[0]],
        },
    )
    result = executor.read(ReadRequest(actor=actor, call=ToolCall(name="ranking")))
    assert [r["id"][0] for r in result["ranking"]] == ["b", "c", "a"]
    result = executor.read(ReadRequest(actor=actor, call=ToolCall(name="ranking", sort_by="cpu")))
    assert [r["id"][0] for r in result["ranking"]] == ["a", "b", "c"]
    assert not backend.actions


@pytest.mark.postgres
async def test_expand_keeps_snapshot_cursor_and_does_not_archive_raw_page(core, backend):
    async def send(number, message):
        receipt = core.accept(str(number), "owner", message, "wecom")
        await core.process(core.claim(control=message == "清空上下文"))
        with core.factory() as db:
            body = db.scalar(
                select(Outbox.body).where(
                    Outbox.task_id == receipt["task_id"], Outbox.kind == "result"
                )
            )
            context = dict(db.get(ChatSession, "owner").context)
        return core.task(receipt["task_id"]), body, context

    backend.records = [f"line-{i} password=private" for i in range(300)]
    _, summary, before = await send(1, "日志 博客")
    assert "日志摘要" in summary and "private" not in summary
    backend.records.append("new-log-after-snapshot")
    task, expanded, after = await send(2, "展开日志")
    assert after["cursor"] == before["cursor"]
    assert "line-200 " in expanded and "line-299 " in expanded
    assert "new-log-after-snapshot" not in expanded and "private" not in expanded
    assert (
        "日志摘要" in task["result"]
    )  # Audit holds summary; full page is transient outbox/context.
    _, _, page2 = await send(3, "下一页")
    assert "line-100 " in page2["log_page"]["text"] and "line-200 " not in page2["log_page"]["text"]
    await send(4, "清空上下文")
    failed, _, _ = await send(5, "展开日志")
    assert failed["status"] == "failed"


@pytest.mark.postgres
async def test_expand_rejects_changed_container_and_expired_snapshot(core, backend):
    async def send(i, msg):
        task = core.accept(str(i), "owner", msg)
        await core.process(core.claim())
        return core.task(task["task_id"])

    await send(1, "日志 博客")
    backend.instances["blog/web"][0]["id"] = "d" * 64
    assert "容器已变化" in (await send(2, "展开日志"))["result"]
    backend.instances["blog/web"][0]["id"] = "a" * 64
    with core.factory.begin() as db:
        session = db.get(ChatSession, "owner")
        context = dict(session.context)
        context["log_page"] = {
            **context["log_page"],
            "expires_at": (utcnow() - timedelta(seconds=1)).isoformat(),
        }
        session.context = context
    assert "没有有效" in (await send(3, "展开日志"))["result"]


@pytest.mark.postgres
async def test_cached_log_page_is_not_sent_to_intent_model(core):
    task = core.accept("logs", "owner", "日志 博客")
    await core.process(core.claim())

    class Router:
        async def route(self, message, context, catalog):
            assert "log_page" not in context
            return Intent(kind="clarify", message="请明确目标")

    core.router = Router()
    task = core.accept("route", "owner", "这是什么意思")
    await core.process(core.claim())
    assert core.task(task["task_id"])["status"] == "succeeded"
