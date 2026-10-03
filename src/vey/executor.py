from __future__ import annotations

import secrets
import time
from datetime import timedelta

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from vey.config import Policy, Settings
from vey.db import ExecutorSession, Grant, LogCursor, uid
from vey.domain import (
    Action,
    Actor,
    ConfirmRequest,
    PrepareRequest,
    ReadRequest,
    VeyError,
    digest,
    utcnow,
)
from vey.logs import page_logs, sanitize_records
from vey.system_info import collect_system


class Executor:
    def __init__(self, factory, policy: Policy, backend, settings: Settings):
        self.factory, self.policy, self.backend, self.settings = factory, policy, backend, settings
        self.policy_hash = digest(policy.model_dump())

    def sync_actor(self, actor: Actor) -> dict:
        now = utcnow()
        if (
            actor.user_id != self.policy.owner_id
            or actor.expires_at <= now
            or actor.expires_at > now + timedelta(minutes=16)
            or actor.started_at > now + timedelta(seconds=30)
        ):
            raise VeyError("invalid_actor", "身份或会话期限无效", 403)
        with self.factory.begin() as db:
            db.execute(
                insert(ExecutorSession).values(**actor.model_dump()).on_conflict_do_nothing()
            )
            current = db.get(ExecutorSession, actor.user_id, with_for_update=True)
            if current.generation != actor.generation:
                if current.started_at >= actor.started_at:
                    raise VeyError("stale_session", "旧会话不能恢复", 409)
                current.generation, current.started_at = actor.generation, actor.started_at
                db.execute(
                    update(Grant)
                    .where(Grant.user_id == actor.user_id, Grant.status == "pending")
                    .values(status="cancelled", updated_at=now)
                )
                db.execute(delete(LogCursor).where(LogCursor.user_id == actor.user_id))
            current.expires_at = (
                max(current.expires_at, actor.expires_at)
                if current.generation == actor.generation
                else actor.expires_at
            )
        return {"status": "ok"}

    def _actor(self, db, actor: Actor):
        current = db.get(ExecutorSession, actor.user_id)
        if (
            actor.user_id != self.policy.owner_id
            or not current
            or current.generation != actor.generation
            or current.expires_at <= utcnow()
        ):
            raise VeyError("expired_session", "会话已失效，请重新发送请求", 403)

    def _target(self, name: str, modifying=False):
        service = self.policy.resolve(name)
        instances = self.backend.resolve(service)
        if modifying:
            protected = (
                service.protected or service.key.split("/")[0] in self.policy.protected_projects
            )
            protected = protected or any(
                c["id"] in self.policy.protected_container_ids
                or c.get("project") in self.policy.protected_projects
                for c in instances
            )
            if protected:
                raise VeyError(
                    "protected_service", "该服务为受保护对象，启动、停止和重启均被拒绝", 403
                )
            if not instances:
                raise VeyError("not_deployed", "目标容器尚未创建，本版本不执行部署")
        return service, instances

    def prepare(self, request: PrepareRequest) -> dict:
        with self.factory() as db:
            self._actor(db, request.actor)
        service, instances = self._target(request.target, modifying=True)
        with self.factory.begin() as db:
            self._actor(db, request.actor)
            prior = db.scalar(select(Grant).where(Grant.task_id == request.task_id))
            if prior:
                return self._grant(prior)
            grant = Grant(
                code=secrets.token_hex(4).upper(),
                task_id=request.task_id,
                user_id=request.actor.user_id,
                generation=request.actor.generation,
                target=service.key,
                action=request.action.value,
                container_ids=sorted(c["id"] for c in instances),
                policy_hash=self.policy_hash,
                expires_at=utcnow() + timedelta(minutes=2),
            )
            db.add(grant)
            db.flush()
            return self._grant(grant)

    def _grant(self, grant: Grant) -> dict:
        return {
            "code": grant.code,
            "task_id": grant.task_id,
            "target": grant.target,
            "action": grant.action,
            "instances": len(grant.container_ids),
            "container_ids": grant.container_ids,
            "status": grant.status,
            "expires_at": grant.expires_at.isoformat(),
            "result": grant.result,
        }

    def cancel(self, request: ConfirmRequest) -> dict:
        with self.factory.begin() as db:
            self._actor(db, request.actor)
            grant = db.get(Grant, request.code, with_for_update=True)
            self._check_owner(grant, request.actor)
            if grant.status != "pending":
                raise VeyError("already_started", "此操作已经处理，无法撤销", 409)
            grant.status, grant.updated_at = "cancelled", utcnow()
            return self._grant(grant)

    def _check_owner(self, grant: Grant | None, actor: Actor):
        if not grant or grant.user_id != actor.user_id or grant.generation != actor.generation:
            raise VeyError("unknown_confirmation", "确认编号不存在或不属于当前会话", 404)

    def confirm(self, request: ConfirmRequest) -> dict:
        with self.factory.begin() as db:
            self._actor(db, request.actor)
            grant = db.get(Grant, request.code, with_for_update=True)
            self._check_owner(grant, request.actor)
            if grant.status != "pending":
                return self._grant(grant)
            if grant.expires_at <= utcnow():
                raise VeyError("expired_confirmation", "确认已超过 2 分钟，请重新提出操作", 409)
            # Serialize the check-and-mark transition across workers/processes.
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:target, 0))"),
                {"target": grant.target},
            )
            busy = db.scalar(
                select(Grant.code).where(
                    Grant.target == grant.target, Grant.status.in_(["executing", "unknown"])
                )
            )
            if busy:
                raise VeyError(
                    "service_busy", "该服务有执行中或结果未知的操作，请先核对操作记录", 409
                )
            service, instances = self._target(grant.target, modifying=True)
            if (
                sorted(c["id"] for c in instances) != grant.container_ids
                or grant.policy_hash != self.policy_hash
            ):
                raise VeyError(
                    "target_changed", "目标实例或权限配置变化，请重新提出操作并确认", 409
                )
            grant.status, grant.updated_at = "executing", utcnow()
            ids, action, code = list(grant.container_ids), Action(grant.action), grant.code
        # The durable executing marker is committed BEFORE any external side effect.
        results, uncertain = [], False
        for container_id in ids:
            try:
                self.backend.mutate(container_id, action)
                state = self.backend.inspect(container_id)
                expected = "exited" if action == Action.STOP else "running"
                valid = state["status"] == expected and (
                    action == Action.STOP
                    or state.get("health") in {None, "healthy", "not_configured"}
                )
                results.append(
                    {
                        "id": container_id,
                        "operation": "completed",
                        "verification": state,
                        "expected_state_observed": valid,
                    }
                )
            except Exception as exc:
                # SDK errors may arrive after the daemon has received the command.
                results.append(
                    {"id": container_id, "operation": "unknown", "error_type": type(exc).__name__}
                )
                uncertain = True
                break
        all_ok = len(results) == len(ids) and all(r.get("expected_state_observed") for r in results)
        business = {"status": "not_checked"}
        if all_ok and action != Action.STOP:
            try:
                business = self.backend.health(service)
            except Exception:
                business = {"status": "unreachable"}
            all_ok = business.get("status") in {"passed", "not_configured"}
        status = "unknown" if uncertain else "succeeded" if all_ok else "partial"
        with self.factory.begin() as db:
            grant = db.get(Grant, code, with_for_update=True)
            grant.status, grant.result, grant.updated_at = (
                status,
                {
                    "instances": results,
                    "business_health": business,
                    "note": "容器状态不等于业务恢复；未尝试的实例没有执行",
                },
                utcnow(),
            )
            return self._grant(grant)

    def operation_status(self, actor: Actor, task_id: str) -> dict:
        with self.factory() as db:
            self._actor(db, actor)
            grant = db.scalar(
                select(Grant).where(Grant.task_id == task_id, Grant.user_id == actor.user_id)
            )
            if not grant:
                return {"status": "not_found"}
            result = self._grant(grant)
            ids = list(grant.container_ids)
        if result["status"] in {"unknown", "executing"}:
            try:
                result["current_state"] = [self.backend.inspect(i) for i in ids]
            except Exception:
                result["current_state"] = "unavailable"
            result["note"] = (
                "当前状态不能证明重启是否发生；结果未知的操作不自动重试，需要管理员核对"
            )
        return result

    def read(self, request: ReadRequest) -> dict:
        with self.factory() as db:
            self._actor(db, request.actor)
        call = request.call
        if call.name == "system":
            result = collect_system(
                self.settings.host_proc,
                self.settings.host_disk_paths,
                self.settings.host_disk_labels,
                self.policy.thresholds,
            )
            result["container_anomalies"] = [
                {
                    "target": s.key,
                    "name": c.get("name", c["id"][:12]),
                    "status": c.get("status"),
                    "health": c.get("health"),
                }
                for s in self.policy.services
                for c in self.backend.resolve(s)
                if c.get("status") != "running" or c.get("health") == "unhealthy"
            ]
            return result
        if call.name == "ranking":
            return self._ranking(call.sort_by)
        if call.name == "services":
            return {
                "services": [
                    {"target": s.key, "instances": self.backend.resolve(s)}
                    for s in self.policy.services
                ]
            }
        if call.cursor:
            return self._next_log(request)
        service, instances = self._target(call.target)
        if call.name == "inspect":
            return {"target": service.key, "instances": instances, "deployed": bool(instances)}
        if call.name == "health":
            return {"target": service.key, **self.backend.health(service)}
        if call.name == "stats":
            return {
                "target": service.key,
                "instances": [
                    {"name": c.get("name"), **self.backend.stats(c["id"])} for c in instances
                ],
            }
        if not instances:
            raise VeyError("not_deployed", "没有可读取日志的容器")
        records, boundary = self.backend.logs([c["id"] for c in instances], call)
        records = sanitize_records(records)
        return self._log_page(
            request.actor, service.key, [c["id"] for c in instances], records, call.lines, boundary
        )

    def _log_page(self, actor, target, ids, records, lines, boundary, pending=None):
        page = page_logs(records, lines, pending=pending)
        cursor_id = None
        with self.factory.begin() as db:
            self._actor(db, actor)
            db.execute(delete(LogCursor).where(LogCursor.expires_at <= utcnow()))
            if page.remaining or page.pending:
                # Bound memory/storage even when a user repeatedly starts new queries.
                count = db.scalar(
                    select(func.count())
                    .select_from(LogCursor)
                    .where(LogCursor.user_id == actor.user_id)
                )
                if count >= 10:
                    raise VeyError("cursor_limit", "分页缓存已达上限，请清空上下文或等待过期")
                cursor_id = uid()
                db.add(
                    LogCursor(
                        id=cursor_id,
                        user_id=actor.user_id,
                        generation=actor.generation,
                        target=target,
                        container_ids=ids,
                        payload={
                            "records": page.remaining,
                            "pending": page.pending,
                            "boundary": boundary,
                        },
                        expires_at=actor.expires_at,
                    )
                )
        return {
            "target": target,
            "container_ids": ids,
            "sampled_at": utcnow().isoformat(),
            "text": page.text,
            "cursor": cursor_id,
            "truncated": page.truncated,
            "truncation_reason": page.reason,
            "source_window_limited": boundary,
            "message": f"日志已截断（{'10000 字符' if page.reason == 'characters' else '500 行'}上限），可继续翻页"
            if page.truncated
            else "可继续查看更早日志"
            if cursor_id
            else "到达本次可读窗口边界；更早日志请指定时间范围"
            if boundary
            else "已到日志末页",
        }

    def _ranking(self, sort_by):
        started = time.monotonic()
        candidates, failures, rows = {}, [], []
        for service in self.policy.services:
            for c in self.backend.resolve(service):
                candidates[c["id"]] = {"target": service.key, **c}
        # Explicitly protected legacy containers have no Compose labels, but can be read.
        for container_id in self.policy.protected_container_ids:
            if container_id not in candidates:
                try:
                    candidates[container_id] = {
                        "target": "受保护容器",
                        **self.backend.inspect(container_id),
                    }
                except Exception:
                    failures.append({"id": container_id[:12], "reason": "容器不可读取"})
        skipped = 0
        for c in candidates.values():
            if len(rows) >= 50 or time.monotonic() - started >= 12:
                skipped += 1
                continue
            if c.get("status") != "running":
                failures.append({"id": c["id"][:12], "reason": "未运行，未采集资源"})
                continue
            try:
                rows.append(
                    {"target": c["target"], "name": c.get("name"), **self.backend.stats(c["id"])}
                )
            except Exception:
                failures.append({"id": c["id"][:12], "reason": "资源采集失败"})
        metric = "memory_bytes" if sort_by == "memory" else "cpu_percent"
        rows.sort(key=lambda r: r.get(metric) if r.get(metric) is not None else -1, reverse=True)
        return {
            "ranking": rows,
            "sort_by": sort_by,
            "failures": failures,
            "skipped": skipped,
            "sampled_at": utcnow().isoformat(),
            "scope": "仅已配置服务和受保护容器；逐个瞬时采样",
        }

    def _next_log(self, request):
        with self.factory.begin() as db:
            self._actor(db, request.actor)
            cursor = db.get(LogCursor, request.call.cursor, with_for_update=True)
            if (
                not cursor
                or cursor.user_id != request.actor.user_id
                or cursor.generation != request.actor.generation
                or cursor.expires_at <= utcnow()
            ):
                raise VeyError("expired_cursor", "日志游标已失效，请重新查询", 409)
            _, instances = self._target(cursor.target)
            if sorted(c["id"] for c in instances) != sorted(cursor.container_ids):
                raise VeyError("target_changed", "容器已替换，请重新查询日志", 409)
            target, ids, payload = cursor.target, list(cursor.container_ids), cursor.payload
            db.delete(cursor)
        return self._log_page(
            request.actor,
            target,
            ids,
            payload["records"],
            request.call.lines,
            payload["boundary"],
            payload.get("pending"),
        )

    def cleanup(self):
        now = utcnow()
        with self.factory.begin() as db:
            db.execute(delete(LogCursor).where(LogCursor.expires_at <= now))
            db.execute(
                update(Grant)
                .where(Grant.status == "pending", Grant.expires_at <= now)
                .values(status="expired", updated_at=now)
            )
            db.execute(
                delete(Grant).where(
                    Grant.created_at < now - timedelta(days=30),
                    Grant.status.not_in(["executing", "unknown"]),
                )
            )

    def recover(self):
        with self.factory.begin() as db:
            db.execute(
                update(Grant)
                .where(Grant.status == "executing")
                .values(
                    status="unknown",
                    updated_at=utcnow(),
                    result={"note": "执行器重启，操作结果需核对；未自动重试"},
                )
            )
