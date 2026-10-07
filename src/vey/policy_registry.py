"""Executor-owned configuration authority. Publish and Docker effects share a PG lock."""

import hashlib
import hmac
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

from pydantic import Field
from sqlalchemy import select, text, update

from vey.config import Policy, Service
from vey.db import Grant, PolicyState, PolicyVersion, uid
from vey.domain import StrictModel, VeyError, digest, utcnow

LOCK = 764392821


class Change(StrictModel):
    base_revision: str = Field(pattern=r"^[a-f0-9]{32}$")
    services: list[Service] = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=1, max_length=300)
    rollback_of: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class Publication(Change):
    proof: str = Field(max_length=100)


def validate_services(base: Policy, services: list[Service]) -> Policy:
    baseline = {s.key: s for s in base.services}
    candidate = {s.key: s for s in services}
    for key, service in baseline.items():
        if service.protected or key.split("/")[0] in base.protected_projects:
            if key not in candidate or not candidate[key].protected:
                raise VeyError("protected_policy", "核心保护项不能删除或解除保护", 403)
    names = set()
    for service in services:
        if len(service.key) > 120 or len(service.aliases) > 8:
            raise VeyError("invalid_policy", "服务名过长或别名超过 8 个")
        for name in [service.key, *service.aliases]:
            normalized = name.strip().casefold()
            if (
                not normalized
                or normalized != name.casefold()
                or len(name) > 120
                or normalized in names
            ):
                raise VeyError("ambiguous_alias", "服务名与别名须唯一，不能空白或包含首尾空格")
            names.add(normalized)
        if service.health_url:
            url = urlsplit(service.health_url)
            try:
                port = url.port
            except ValueError:
                raise VeyError("invalid_health_url", "健康检查端口无效") from None
            if (
                len(service.health_url) > 512
                or url.query
                or url.fragment
                or url.password
                or port == 0
            ):
                raise VeyError(
                    "invalid_health_url", "健康检查地址最多 512 字符，不能包含凭据、查询参数或片段"
                )
    return Policy.model_validate(
        {**base.model_dump(), "services": [s.model_dump() for s in services]}
    )


class PolicyRegistry:
    def __init__(self, factory, baseline, backend, secret, clock=time.time):
        if len(secret) < 32:
            raise ValueError("Independent management token must have 32+ characters")
        self.factory, self.baseline, self.backend = factory, baseline, backend
        self.secret, self.clock = secret, clock

    @contextmanager
    def transaction(self, exclusive=False):
        # This factory must use a separate pool from Executor.factory: a lock waiter
        # must never occupy a connection needed to commit an executing marker.
        with self.factory.begin() as db:
            db.execute(text("SET LOCAL lock_timeout='5s'"))
            name = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
            db.execute(text(f"SELECT {name}(:key)"), {"key": LOCK})
            yield db

    def initialize(self):
        with self.transaction(exclusive=True) as db:
            if not db.get(PolicyState, 1):
                services = [s.model_dump() for s in self.baseline.services]
                # Project protection is made explicit in the editable projection.
                for s in services:
                    s["protected"] |= s["key"].split("/")[0] in self.baseline.protected_projects
                version = PolicyVersion(id=uid(), services=services, reason="初始化文件基线")
                db.add(version)
                db.flush()
                db.add(
                    PolicyState(
                        id=1, active_id=version.id, baseline_hash=digest(self.baseline.model_dump())
                    )
                )
            else:
                self.current(db)

    def current(self, db):
        state = db.get(PolicyState, 1)
        if not state or state.baseline_hash != digest(self.baseline.model_dump()):
            raise VeyError(
                "policy_baseline_changed", "配置基线未初始化或已变化，需要管理员核对", 503
            )
        version = db.get(PolicyVersion, state.active_id)
        policy = validate_services(
            self.baseline, [Service.model_validate(s) for s in version.services]
        )
        return policy, version.id

    @contextmanager
    def guard(self):
        with self.transaction() as db:
            yield self.current(db)

    def catalog(self):
        with self.guard() as (policy, revision):
            return {"policy": policy.model_dump(), "revision": revision}

    def listing(self):
        with self.transaction() as db:
            policy, revision = self.current(db)
            versions = db.scalars(
                select(PolicyVersion).order_by(PolicyVersion.created_at.desc()).limit(100)
            ).all()
            return {
                "revision": revision,
                "services": [s.model_dump() for s in policy.services],
                "protected_projects": policy.protected_projects,
                "locked_services": [
                    s.key
                    for s in self.baseline.services
                    if s.protected or s.key.split("/")[0] in policy.protected_projects
                ],
                "history": [
                    {
                        "id": v.id,
                        "parent_id": v.parent_id,
                        "reason": v.reason,
                        "rollback_of": v.rollback_of,
                        "created_at": v.created_at.isoformat(),
                        "services": v.services,
                    }
                    for v in versions
                ],
                "history_limit": 100,
            }

    def check(self, db, change):
        current, revision = self.current(db)
        if change.base_revision != revision:
            raise VeyError("stale_policy", "配置已被更新，请刷新后重新校验", 409)
        candidate = validate_services(self.baseline, change.services)
        values = [s.model_dump() for s in candidate.services]
        if not change.reason.strip():
            raise VeyError("reason_required", "请填写变更原因")
        if change.rollback_of:
            historical = db.get(PolicyVersion, change.rollback_of)
            if not historical or historical.services != values:
                raise VeyError("invalid_rollback", "回滚内容必须与所选版本一致", 409)
        if values == [s.model_dump() for s in current.services]:
            raise VeyError("unchanged_policy", "配置内容没有变化")
        existing = {s.key for s in current.services} | {s.key for s in self.baseline.services}
        inventory, warnings = {}, []
        deadline = time.monotonic() + 15
        for service in candidate.services:
            if time.monotonic() > deadline:
                raise VeyError("validation_timeout", "服务检查超过时间预算，请缩小变更后重试", 503)
            inventory[service.key] = sorted(c["id"] for c in self.backend.resolve(service))
            if not inventory[service.key]:
                if service.key not in existing:
                    raise VeyError("not_deployed", "新增服务未匹配到 Compose 容器：" + service.key)
                warnings.append(service.key + "：历史登记项暂无 Compose 容器")
        old = {s.key: s.model_dump() for s in current.services}
        new = {s.key: s.model_dump() for s in candidate.services}
        differences = [
            {"key": k, "before": old.get(k), "after": new.get(k)}
            for k in sorted(old.keys() | new.keys())
            if old.get(k) != new.get(k)
        ]
        if list(old) != list(new):
            differences.append({"key": "服务顺序", "before": list(old), "after": list(new)})
        return candidate, inventory, warnings, differences

    def signature(self, change, inventory, expires):
        value = digest(
            {
                "change": Change.model_validate(change.model_dump(exclude={"proof"})).model_dump(),
                "inventory": inventory,
                "expires": expires,
            }
        )
        return hmac.new(self.secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    def preview(self, change):
        with self.transaction() as db:
            _, inventory, warnings, differences = self.check(db, change)
            expires = int(self.clock()) + 300
            return {
                "proof": f"{expires}.{self.signature(change, inventory, expires)}",
                "expires_at": expires,
                "diff": differences,
                "warnings": warnings,
                "effect": "发布或回滚会作废全部待确认操作；进行中的操作结束前不会发布",
            }

    def publish(self, change):
        with self.transaction(exclusive=True) as db:
            candidate, inventory, _, _ = self.check(db, change)
            try:
                expires, signature = change.proof.split(".", 1)
                expires = int(expires)
            except ValueError:
                raise VeyError("invalid_preview", "请先校验并确认变更") from None
            if not self.clock() <= expires <= self.clock() + 300 or not hmac.compare_digest(
                signature, self.signature(change, inventory, expires)
            ):
                raise VeyError(
                    "invalid_preview", "校验已过期，或内容／容器发生变化，请重新校验", 409
                )
            # A crashed executor leaves executing/unknown markers; refuse publication until
            # recovery/reconciliation rather than claim an in-flight effect has been revoked.
            if db.scalar(
                select(Grant.code).where(Grant.status.in_(["executing", "unknown"])).limit(1)
            ):
                raise VeyError(
                    "operation_in_progress", "仍有执行中或结果未知的操作，请先人工核对后发布", 409
                )
            version = PolicyVersion(
                id=uid(),
                parent_id=change.base_revision,
                services=[s.model_dump() for s in candidate.services],
                reason=change.reason.strip(),
                rollback_of=change.rollback_of,
            )
            db.add(version)
            db.flush()
            db.get(PolicyState, 1).active_id = version.id
            cancelled = db.execute(
                update(Grant)
                .where(Grant.status == "pending")
                .values(status="cancelled", updated_at=utcnow())
            ).rowcount
            return {
                "revision": version.id,
                "cancelled_confirmations": cancelled,
                "status": "published",
            }
