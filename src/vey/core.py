from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from vey.db import ChatSession, Event, Outbox, Task, uid
from vey.domain import (
    Actor,
    ConfirmRequest,
    PrepareRequest,
    ReadRequest,
    ToolCall,
    VeyError,
    digest,
    utcnow,
)
from vey.model import deterministic_intent
from vey.security import redact, safe_value
from vey.wecom import delivery_parts

CONTROL = re.compile(
    r"^(?:清空上下文|(?:确认|取消)\s+[A-Fa-f0-9]{8}|(?:任务|操作)\s+[a-f0-9]{32})$"
)
TERMINAL = {"succeeded", "failed", "cancelled", "expired", "partial", "unknown"}
CHECK_TIMEOUT_SECONDS = 60


class Core:
    def __init__(self, factory, policy, executor, model, router=None):
        self.factory, self.policy, self.executor, self.model = factory, policy, executor, model
        self.router = router

    def accept(self, message_id, user_id, message, channel="debug"):
        if user_id != self.policy.owner_id:
            raise VeyError("unauthorized_user", "仅限配置的本人账号使用", 403)
        if not message or len(message) > 4000:
            raise VeyError("invalid_message", "消息必须为 1～4000 字符")
        now = utcnow()
        with self.factory.begin() as db:
            # Serialize session changes and duplicate-message admission for this owner.
            db.execute(
                insert(ChatSession)
                .values(
                    user_id=user_id,
                    generation=uid(),
                    started_at=now,
                    expires_at=now + timedelta(minutes=15),
                    context={},
                )
                .on_conflict_do_nothing()
            )
            session = db.get(ChatSession, user_id, with_for_update=True)
            duplicate = db.scalar(select(Task).where(Task.message_id == message_id))
            if duplicate:
                return {"task_id": duplicate.id, "status": duplicate.status, "duplicate": True}
            if session.expires_at <= now or message.strip() == "清空上下文":
                session.generation, session.started_at, session.context = uid(), now, {}
                db.execute(update(Task).where(Task.user_id == user_id).values(text=""))
            session.expires_at = now + timedelta(minutes=15)
            task = Task(
                message_id=message_id,
                user_id=user_id,
                generation=session.generation,
                text=redact(message),
                channel=channel,
                status="control_queued" if CONTROL.fullmatch(message.strip()) else "queued",
            )
            db.add(task)
            db.flush()
            if channel == "wecom":
                db.add(
                    Outbox(
                        task_id=task.id,
                        kind="received",
                        user_id=user_id,
                        body=f"已受理，任务编号：{task.id}。处理完成后将发送结果。",
                    )
                )
            return {"task_id": task.id, "status": task.status, "duplicate": False}

    def claim(self, control=False):
        with self.factory.begin() as db:
            task = db.scalar(
                select(Task)
                .where(Task.status == ("control_queued" if control else "queued"))
                .order_by(Task.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not task:
                return None
            task.status, task.lease_until, task.updated_at = (
                "running",
                utcnow() + timedelta(seconds=120),
                utcnow(),
            )
            return task.id

    def task(self, task_id):
        with self.factory() as db:
            task = db.get(Task, task_id)
            if not task:
                raise VeyError("task_not_found", "任务不存在", 404)
            return {
                "id": task.id,
                "status": task.status,
                "result": task.result,
                "created_at": task.created_at.isoformat(),
            }

    def _input(self, task_id):
        with self.factory() as db:
            task = db.get(Task, task_id)
            session = db.get(ChatSession, task.user_id)
            if (
                not session
                or session.generation != task.generation
                or session.expires_at <= utcnow()
            ):
                raise VeyError("expired_session", "任务对应会话已失效，请重新发送请求")
            actor = Actor(
                user_id=task.user_id,
                generation=session.generation,
                expires_at=session.expires_at,
                started_at=session.started_at,
            )
            return actor, task.text, dict(session.context)

    def event(self, task_id, kind, data):
        clean = safe_value(data)
        if len(json.dumps(clean, ensure_ascii=False, default=str)) > 12000:
            clean = {"summary": "事件内容过大，保留摘要哈希", "digest": digest(clean)}
        with self.factory.begin() as db:
            db.add(Event(task_id=task_id, kind=kind, data=clean))

    def _finish(self, task_id, status, message, context_update=None):
        with self.factory.begin() as db:
            task = db.get(Task, task_id, with_for_update=True)
            session = db.get(ChatSession, task.user_id, with_for_update=True)
            valid = (
                session and session.generation == task.generation and session.expires_at > utcnow()
            )
            if not valid:
                status, message = (
                    "cancelled",
                    "任务所属上下文已失效，未恢复旧会话；已执行操作可通过任务编号查询",
                )
            task.status, task.updated_at, task.lease_until = status, utcnow(), None
            # Persist a bounded summary, not a permanent archive of raw logs/chat.
            task.result = redact(message)[:3000]
            if valid:
                context = dict(session.context)
                if context_update:
                    context.update(context_update)
                history = context.get("history", [])[-4:]
                history.append({"user": task.text[:1000], "result": task.result[:1000]})
                context["history"] = history
                session.context = context
            if task.channel == "wecom":
                db.execute(
                    insert(Outbox)
                    .values(
                        id=uid(),
                        task_id=task.id,
                        kind="result",
                        user_id=task.user_id,
                        body=redact(message),
                        status="pending",
                        attempts=0,
                        next_at=utcnow(),
                    )
                    .on_conflict_do_nothing()
                )

    async def process(self, task_id):
        evidence, calls, context_update = [], set(), {}
        model_metrics_start = len(getattr(self.model, "metrics", []))
        is_control = False
        try:
            actor, message, context = self._input(task_id)
            is_control = bool(CONTROL.fullmatch(message.strip()))
            await self.executor.sync_actor(actor)
            if is_control:
                await self._control(task_id, actor, message.strip())
                return
            async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
                intent = deterministic_intent(message, self.policy, context)
                if intent is None:
                    intent = await (self.router or self.model).route(
                        message, context, self.policy.catalog()
                    )
                self.event(task_id, "intent", intent.model_dump(mode="json"))
                # Check session again before admitting any action after a model call.
                self._input(task_id)
                if intent.kind == "clarify":
                    self._finish(task_id, "succeeded", intent.message or "请明确项目和服务名称")
                    return
                if intent.target:
                    context_update["target"] = self.policy.resolve(intent.target).key
                if intent.kind == "operation":
                    grant = await self.executor.prepare(
                        PrepareRequest(
                            actor=actor, task_id=task_id, target=intent.target, action=intent.action
                        )
                    )
                    self.event(task_id, "operation_prepared", grant)
                    self._finish(
                        task_id,
                        "awaiting_confirmation",
                        f"将对本机 Ubuntu 的 {grant['target']} 执行 {grant['action']}，共 {grant['instances']} 个实例。可能短暂中断服务。\n容器 ID：{', '.join(i[:12] for i in grant['container_ids'])}\n回复「确认 {grant['code']}」执行，或「取消 {grant['code']}」。2 分钟内有效。",
                        context_update,
                    )
                    return
                if intent.kind == "query":
                    call = intent.tool or ToolCall(name="inspect", target=intent.target)
                    result = await self._read(task_id, actor, call, evidence)
                    context_update.update(
                        {
                            "cursor": result.get("cursor"),
                            "target": result.get("target", context.get("target")),
                        }
                    )
                    self._finish(task_id, "succeeded", render_result(result), context_update)
                    return
                target = context_update.get("target") or context.get("target")
                summary = ""
                for _ in range(5):
                    self._input(task_id)
                    step = await self.model.next_step(
                        message, evidence, target, self.policy.catalog()
                    )
                    if step.tool is None:
                        summary = step.summary
                        break
                    fingerprint = digest(step.tool.model_dump(mode="json"))
                    if fingerprint in calls:
                        summary = "检测到重复检查，已停止继续排查。"
                        break
                    calls.add(fingerprint)
                    try:
                        await self._read(task_id, actor, step.tool, evidence)
                    except VeyError as error:
                        evidence.append(
                            {
                                "id": f"E{len(evidence) + 1}",
                                "tool": step.tool.name,
                                "error": error.code,
                                "message": error.message,
                            }
                        )
                else:
                    summary = "已达到 5 次工具调用上限，停止继续排查。"
                self._finish(
                    task_id, "succeeded", render_evidence(evidence, summary), context_update
                )
        except TimeoutError:
            self._finish(
                task_id,
                "partial",
                render_evidence(evidence, "已达到 60 秒检查上限，停止继续排查。"),
                context_update,
            )
        except VeyError as error:
            self.event(task_id, "error", {"code": error.code})
            self._finish(
                task_id,
                "unknown" if error.code == "operation_unknown" else "failed",
                render_evidence(evidence, error.message),
            )
        except Exception as error:
            self.event(task_id, "error", {"type": type(error).__name__})
            self._finish(
                task_id,
                "unknown",
                "任务发生异常，未确认操作成功。请使用任务编号核对，修改请求不会自动重试。",
            )
        finally:
            if not is_control:
                for metric in getattr(self.model, "metrics", [])[model_metrics_start:]:
                    self.event(task_id, "model", metric)
                if hasattr(self.model, "metrics"):
                    self.model.metrics.clear()

    async def _read(self, task_id, actor, call, evidence):
        self._input(task_id)
        started = time.monotonic()
        result = safe_value(await self.executor.read(ReadRequest(actor=actor, call=call)))
        evidence.append(
            {"id": f"E{len(evidence) + 1}", "tool": call.model_dump(mode="json"), "result": result}
        )
        audit = dict(result)
        if "text" in audit:
            audit["log_excerpt"] = audit.pop("text")[:300]
        self.event(
            task_id,
            "tool",
            {
                "call": call.model_dump(mode="json"),
                "result": audit,
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return result

    async def _control(self, task_id, actor, message):
        if message == "清空上下文":
            self._finish(task_id, "succeeded", "上下文和未消费确认已清空；操作审计继续保留。")
            return
        command, value = message.split(maxsplit=1)
        if command == "任务":
            self._finish(task_id, "succeeded", render_result(self.task(value)))
            return
        if command == "操作":
            self._finish(
                task_id, "succeeded", render_result(await self.executor.status(actor, value))
            )
            return
        request = ConfirmRequest(actor=actor, code=value.upper())
        try:
            result = await (
                self.executor.confirm(request)
                if command == "确认"
                else self.executor.cancel(request)
            )
        except VeyError as error:
            if command == "确认" and error.code == "executor_unavailable":
                with self.factory.begin() as db:
                    original_id = db.scalar(
                        select(Event.task_id).where(
                            Event.kind == "operation_prepared",
                            Event.data["code"].as_string() == request.code,
                        )
                    )
                    original = db.get(Task, original_id) if original_id else None
                    if original:
                        original.status, original.result, original.updated_at = (
                            "unknown",
                            "确认请求响应丢失，执行结果未知，请查询操作记录",
                            utcnow(),
                        )
                raise VeyError(
                    "operation_unknown",
                    "确认请求响应丢失，执行结果未知；请查询原操作编号，不要重复提交修改",
                ) from None
            raise
        with self.factory.begin() as db:
            original = db.get(Task, result["task_id"])
            if original and result["status"] != "pending":
                original.status, original.result, original.updated_at = (
                    result["status"],
                    render_result(result)[:3000],
                    utcnow(),
                )
        self.event(task_id, "operation", result)
        self._finish(
            task_id,
            result["status"] if result["status"] in TERMINAL else "succeeded",
            render_result(result),
        )

    def recover(self):
        with self.factory.begin() as db:
            # Do not replay any started task, including confirmations, after a restart.
            interrupted = db.execute(
                update(Task)
                .where(Task.status == "running")
                .values(
                    status="unknown",
                    result="核心服务重启，任务结果需查询核对，未自动重放",
                    lease_until=None,
                    updated_at=utcnow(),
                )
                .returning(Task.id, Task.user_id, Task.channel, Task.result)
            )
            for task in interrupted:
                if task.channel == "wecom":
                    db.execute(
                        insert(Outbox)
                        .values(
                            task_id=task.id,
                            kind="result",
                            user_id=task.user_id,
                            body=f"任务编号：{task.id}\n{task.result}",
                        )
                        .on_conflict_do_nothing()
                    )

    def cleanup(self):
        now = utcnow()
        with self.factory.begin() as db:
            expired = list(
                db.scalars(select(ChatSession.user_id).where(ChatSession.expires_at <= now))
            )
            if expired:
                db.execute(
                    update(ChatSession).where(ChatSession.user_id.in_(expired)).values(context={})
                )
                db.execute(update(Task).where(Task.user_id.in_(expired)).values(text=""))
            db.execute(
                update(Task)
                .where(
                    Task.status == "awaiting_confirmation",
                    Task.updated_at < now - timedelta(minutes=2),
                )
                .values(status="expired", updated_at=now)
            )
            db.execute(
                delete(Task).where(
                    Task.created_at < now - timedelta(days=30),
                    Task.status.in_(TERMINAL | {"awaiting_confirmation"}),
                )
            )
            db.execute(
                update(Outbox)
                .where(Outbox.status == "sent", Outbox.created_at < now - timedelta(minutes=15))
                .values(body="")
            )
            db.execute(
                update(Outbox)
                .where(Outbox.status != "sent", Outbox.created_at < now - timedelta(hours=1))
                .values(status="failed", body="")
            )

    async def loop(self, control=False):
        while True:
            try:
                task_id = self.claim(control)
                if task_id:
                    await self.process(task_id)
                else:
                    await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(2)

    async def send_pending(self, sender):
        with self.factory.begin() as db:
            item = db.scalar(
                select(Outbox)
                .where(
                    Outbox.status.in_(["pending", "sending"]),
                    Outbox.next_at <= utcnow(),
                )
                .order_by(Outbox.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not item:
                return False
            # A process can disappear during its last allowed attempt. Do not leave
            # that row in 'sending', or retry beyond the per-part budget.
            if item.attempts >= 10 or item.created_at < utcnow() - timedelta(hours=1):
                item.status = "failed"
                if item.created_at < utcnow() - timedelta(hours=1):
                    item.body = ""
                return True
            parts = delivery_parts(item.body, item.task_id, item.kind)
            if item.sent_parts >= len(parts):
                item.status = "sent"
                return True
            item.status, item.attempts, item.next_at = (
                "sending",
                item.attempts + 1,
                utcnow() + timedelta(seconds=60),
            )
            item_id, user = item.id, item.user_id
            part_index, attempt = item.sent_parts, item.attempts
            body = parts[part_index]
        try:
            await sender.send(user, body)
            status = "sent"
        except Exception:
            status = "pending"
        with self.factory.begin() as db:
            item = db.get(Outbox, item_id, with_for_update=True)
            if (
                not item
                or item.status != "sending"
                or item.sent_parts != part_index
                or item.attempts != attempt
            ):
                return True
            if status == "sent":
                item.sent_parts += 1
                item.status = "sent" if item.sent_parts == len(parts) else "pending"
                item.attempts = 0
                item.next_at = utcnow()
            else:
                item.status = "failed" if item.attempts >= 10 else "pending"
                item.next_at = utcnow() + timedelta(seconds=min(300, 2**item.attempts))
        return True


def render_result(result):
    if "text" in result:
        return f"{result.get('target', '')}\n{result['text']}\n{result.get('message', '')}".strip()
    return json.dumps(safe_value(result), ensure_ascii=False, indent=2, default=str)


def render_evidence(evidence, summary):
    text = redact(summary or "已停止检查，以下是现有证据；尚未确认根因。")
    if not evidence:
        return text + "\n尚未取得工具证据。"
    for item in evidence:
        text += (
            f"\n[{item['id']}] "
            + json.dumps(safe_value(item.get("result", item)), ensure_ascii=False, default=str)[
                :1200
            ]
        )
    return text
