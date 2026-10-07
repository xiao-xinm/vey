import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from vey.config import Service
from vey.db import Grant, PolicyVersion
from vey.domain import Action, ConfirmRequest, PrepareRequest, VeyError, digest
from vey.executor import Executor
from vey.executor_app import create_app
from vey.policy_registry import LOCK, Change, PolicyRegistry, Publication, validate_services


@pytest.fixture
def registry(db_factory, policy, backend):
    result = PolicyRegistry(db_factory, policy, backend, "management-" + "x" * 40)
    result.initialize()
    return result


def proposed(registry, alias="新别名"):
    current = registry.listing()
    current["services"][0]["aliases"].append(alias)
    return Change(
        base_revision=current["revision"], services=current["services"], reason="测试别名发布"
    )


def publish(registry, change):
    preview = registry.preview(change)
    return registry.publish(Publication(**change.model_dump(), proof=preview["proof"]))


def test_protected_floor_and_alias_health_validation(policy):
    values = [s.model_copy(deep=True) for s in policy.services]
    values[1].protected = False
    with pytest.raises(VeyError, match="保护"):
        validate_services(policy, values)
    with pytest.raises(VeyError, match="保护"):
        validate_services(policy, [policy.services[0]])
    values = [s.model_copy(deep=True) for s in policy.services]
    values[0].aliases.append(values[1].key)
    with pytest.raises(VeyError, match="唯一"):
        validate_services(policy, values)
    values[0].aliases.pop()
    values[0].health_url = "https://example.com/health?token=do-not-store"
    with pytest.raises(VeyError, match="查询参数"):
        validate_services(policy, values)


@pytest.mark.postgres
def test_preview_binds_content_inventory_expiry_and_revision(registry, backend, db_factory):
    change = proposed(registry)
    preview = registry.preview(change)
    tampered = change.model_copy(update={"reason": "tampered"})
    with pytest.raises(VeyError, match="校验已过期"):
        registry.publish(Publication(**tampered.model_dump(), proof=preview["proof"]))
    old_id = backend.instances["blog/web"][0]["id"]
    backend.instances["blog/web"][0]["id"] = "replacement"
    with pytest.raises(VeyError, match="容器发生变化"):
        registry.publish(Publication(**change.model_dump(), proof=preview["proof"]))
    backend.instances["blog/web"][0]["id"] = old_id
    clock = registry.clock
    registry.clock = lambda: clock() + 301
    with pytest.raises(VeyError, match="过期"):
        registry.publish(Publication(**change.model_dump(), proof=preview["proof"]))
    registry.clock = clock
    assert registry.listing()["revision"] == change.base_revision
    change.services.append(Service(key="missing/web"))
    with pytest.raises(VeyError, match="新增服务未匹配"):
        registry.preview(change)
    assert len(registry.listing()["history"]) == 1


@pytest.mark.postgres
async def test_management_token_is_separate_and_old_confirmations_never_revive(
    registry, executor, settings, actor, tmp_path, backend
):
    token_file = tmp_path / "management"
    token_file.write_text(registry.secret)
    settings.policy_management_enabled = True
    settings.policy_management_token_file = token_file
    app = create_app(settings, executor, registry)
    ops = {"Authorization": "Bearer " + settings.executor_token.get_secret_value()}
    management = {"Authorization": "Bearer " + registry.secret}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://executor"
    ) as client:
        assert (await client.get("/management/config", headers=ops)).status_code == 401
        assert (await client.get("/policy", headers=management)).status_code == 401
        assert (await client.post("/confirm", json={}, headers=management)).status_code == 401
        original = registry.listing()
        req = PrepareRequest(
            actor=actor,
            task_id="1" * 32,
            target="blog/web",
            action=Action.RESTART,
            policy_revision=original["revision"],
        )
        grant = (
            await client.post("/prepare", headers=ops, json=req.model_dump(mode="json"))
        ).json()
        change = proposed(registry)
        first = publish(registry, change)
        assert first["cancelled_confirmations"] == 1
        rollback = Change(
            base_revision=first["revision"],
            services=original["services"],
            reason="回滚验证",
            rollback_of=original["revision"],
        )
        final = publish(registry, rollback)
        assert final["revision"] not in {original["revision"], first["revision"]}
        confirm = ConfirmRequest(actor=actor, code=grant["code"])
        result = await client.post("/confirm", headers=ops, json=confirm.model_dump(mode="json"))
        assert result.json()["status"] == "cancelled" and not backend.actions
        assert (
            await client.post("/prepare", headers=ops, json=req.model_dump(mode="json"))
        ).status_code == 409
        req.task_id = "2" * 32
        req.policy_revision = final["revision"]
        grant = (
            await client.post("/prepare", headers=ops, json=req.model_dump(mode="json"))
        ).json()
        confirm.code = grant["code"]
        assert (
            await client.post("/confirm", headers=ops, json=confirm.model_dump(mode="json"))
        ).json()["status"] == "succeeded"
        assert len(backend.actions) == 1
        assert len(registry.listing()["history"]) == 3


@pytest.mark.postgres
def test_database_history_is_append_only_and_baseline_drift_fails_closed(registry, db_factory):
    revision = registry.listing()["revision"]
    with pytest.raises(DBAPIError), db_factory.begin() as db:
        db.execute(
            update(PolicyVersion).where(PolicyVersion.id == revision).values(reason="erase history")
        )
    registry.baseline = registry.baseline.model_copy(
        update={"protected_container_ids": ["changed"]}
    )
    with pytest.raises(VeyError, match="基线"):
        registry.catalog()


@pytest.mark.postgres
def test_unknown_operation_blocks_publication_without_rewriting_history(
    registry, executor, actor, db_factory
):
    grant = executor.prepare(
        PrepareRequest(actor=actor, task_id="5" * 32, target="blog/web", action=Action.RESTART)
    )
    with db_factory.begin() as db:
        db.get(Grant, grant["code"]).status = "unknown"
    change = proposed(registry)
    with pytest.raises(VeyError, match="结果未知"):
        publish(registry, change)
    assert registry.listing()["revision"] == change.base_revision
    with db_factory() as db:
        assert db.get(Grant, grant["code"]).status == "unknown"


@pytest.mark.postgres
def test_publish_waits_for_effect_then_cancels_other_pending_grants(
    registry, db_factory, executor, actor, backend
):
    policy, revision = registry.catalog().values()
    # Use the same request-scoped snapshot/hash as executor_app.invoke.
    from vey.config import Policy

    current = Executor(db_factory, Policy.model_validate(policy), backend, executor.settings)
    current.policy_hash = digest({"policy": policy, "revision": revision})
    first = current.prepare(
        PrepareRequest(actor=actor, task_id="3" * 32, target="blog/web", action=Action.RESTART)
    )
    current.prepare(
        PrepareRequest(actor=actor, task_id="4" * 32, target="blog/web", action=Action.RESTART)
    )
    change = proposed(registry)
    proof = registry.preview(change)["proof"]
    entered, release, publishing = threading.Event(), threading.Event(), threading.Event()
    mutate = backend.mutate

    def blocked_mutate(*args):
        entered.set()
        assert release.wait(4)
        mutate(*args)

    backend.mutate = blocked_mutate

    def confirm():
        with registry.guard():
            return current.confirm(ConfirmRequest(actor=actor, code=first["code"]))

    def publication():
        publishing.set()
        return registry.publish(Publication(**change.model_dump(), proof=proof))

    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(confirm)
        try:
            assert entered.wait(3)
            pending = pool.submit(publication)
            assert publishing.wait(1)
            deadline = time.monotonic() + 2
            waiting = False
            while time.monotonic() < deadline:
                with db_factory() as db:
                    waiting = db.scalar(
                        text(
                            "SELECT EXISTS (SELECT FROM pg_locks WHERE locktype='advisory' AND objid=:key AND NOT granted)"
                        ),
                        {"key": LOCK},
                    )
                if waiting:
                    break
                time.sleep(0.02)
            assert waiting, "Publish must wait on the database lock held through the Docker effect"
            assert not pending.done()
        finally:
            release.set()
        assert running.result(timeout=5)["status"] == "succeeded"
        assert pending.result(timeout=5)["cancelled_confirmations"] == 1
    with db_factory() as db:
        assert sorted(db.scalars(select(Grant.status)).all()) == ["cancelled", "succeeded"]


async def test_core_uses_current_policy_revision_before_preparing(core, monkeypatch):
    # Existing core fixture is a real core with a local executor; changing aliases must
    # affect routing without mutating the shared Core.policy object.
    baseline = core.policy.model_copy(deep=True)
    dynamic = baseline.model_copy(deep=True)
    dynamic.services[0].aliases.append("新博客")
    captured = []

    async def policy():
        return {"policy": dynamic.model_dump(), "revision": "a" * 32}

    original = core.executor.prepare

    async def prepare(request):
        captured.append(request)
        return await original(request)

    monkeypatch.setattr(core.executor, "policy", policy, raising=False)
    monkeypatch.setattr(core.executor, "prepare", prepare)
    result = core.accept("dynamic-policy", "owner", "重启 新博客")
    task = core.claim()
    await core.process(task)
    assert captured, core.task(result["task_id"])
    assert captured[0].target == "blog/web" and captured[0].policy_revision == "a" * 32
    assert (
        core.policy == baseline
        and core.task(result["task_id"])["status"] == "awaiting_confirmation"
    )
