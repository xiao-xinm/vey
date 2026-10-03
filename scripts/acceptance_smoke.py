"""Run inside agent-core against the dedicated vey-acceptance/web fixture only.

Requires the authenticated debug endpoint; starts/stops/restarts only the fixture.
Prints a concise result list, never credentials or raw database URLs.
"""

import argparse
import json
import os
import re
import time
import uuid
from pathlib import Path

import httpx
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from vey.config import Settings
from vey.db import ChatSession, database
from vey.domain import Actor


def database_check(settings):
    engine, factory = database(settings.database_url.get_secret_value())
    with factory() as db:
        role = db.scalar(text("SELECT current_user"))
        assert db.scalar(text("SELECT current_database()")) == "vey"
    own, other = (
        ("vey_core.tasks", "vey_exec.grants")
        if role == "vey_core"
        else ("vey_exec.grants", "vey_core.tasks")
    )
    with factory() as db:
        db.execute(text(f"SELECT * FROM {own} LIMIT 0"))
    for sql in (f"SELECT * FROM {other} LIMIT 0", "CREATE TABLE public.vey_acl_probe (id int)"):
        try:
            with factory() as db:
                db.execute(text(sql))
                db.rollback()
        except ProgrammingError as error:
            assert getattr(error.orig, "sqlstate", None) == "42501"
        else:
            raise AssertionError("Runtime role has excessive database permissions")
    engine.dispose()
    return role


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-only", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    role = database_check(settings)
    if args.database_only:
        print(json.dumps({"status": "passed", "role": role, "schema_isolation": True}))
        return
    assert role == "vey_core"
    assert settings.debug_enabled and os.getuid() != 0
    assert not Path("/var/run/docker.sock").exists()
    assert not os.access(settings.config, os.W_OK)
    policy = settings.policy()
    target = policy.resolve("vey-acceptance/web").key
    protected = [s.key for s in policy.services if s.protected]
    assert protected
    checks = ["core_nonroot_no_docker_socket_readonly_policy", "core_database_permissions"]
    with (
        httpx.Client(base_url="http://127.0.0.1:8080", timeout=35, trust_env=False) as core,
        httpx.Client(
            transport=httpx.HTTPTransport(uds=settings.executor_socket),
            base_url="http://executor",
            timeout=35,
        ) as executor,
    ):
        core.headers["Authorization"] = "Bearer " + settings.debug_token.get_secret_value()
        executor.headers["Authorization"] = "Bearer " + settings.executor_token.get_secret_value()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if (
                    core.get("/health/ready").status_code == 200
                    and executor.get("/health").status_code == 200
                ):
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise AssertionError("Core or executor did not become ready")
        assert (
            core.get(
                "/debug/tasks/missing", headers={"Authorization": "Bearer invalid"}
            ).status_code
            == 401
        )
        assert (
            core.post(
                "/debug/messages",
                json={"message_id": uuid.uuid4().hex, "user_id": "not-owner", "text": "服务列表"},
            ).status_code
            == 403
        )
        checks.append("debug_authentication_and_owner")

        def submit(message, message_id=None):
            response = core.post(
                "/debug/messages",
                json={
                    "message_id": message_id or uuid.uuid4().hex,
                    "user_id": policy.owner_id,
                    "text": message,
                },
            )
            response.raise_for_status()
            return response.json()

        def wait(task_id):
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                response = core.get("/debug/tasks/" + task_id)
                response.raise_for_status()
                result = response.json()
                if result["status"] not in {"queued", "control_queued", "running"}:
                    return result
                time.sleep(0.1)
            raise AssertionError("Fixture task did not finish")

        def task(message):
            return wait(submit(message)["task_id"])

        def actor():
            engine, factory = database(settings.database_url.get_secret_value())
            with factory() as db:
                session = db.get(ChatSession, policy.owner_id)
                result = Actor(
                    user_id=session.user_id,
                    generation=session.generation,
                    expires_at=session.expires_at,
                    started_at=session.started_at,
                )
            engine.dispose()
            return result.model_dump(mode="json")

        def read(call):
            response = executor.post("/read", json={"actor": actor(), "call": call})
            response.raise_for_status()
            return response.json()

        assert task("服务器概况")["status"] == "succeeded"
        system = read({"name": "system"})
        assert system["available"] and system["memory"]["MemTotal"] > 1_000_000_000
        assert system["cpu_percent"] is not None and system["disks"]
        checks.append("real_host_cpu_memory_disk")
        assert task("状态 " + target)["status"] == "succeeded"
        assert read({"name": "inspect", "target": target})["instances"][0]["status"] == "running"
        stats = read({"name": "stats", "target": target})
        assert stats["instances"][0]["memory_bytes"] > 0
        checks.append("compose_discovery_and_live_stats")

        for service in protected:
            for action in ("启动", "停止", "重启"):
                denied = task(action + " " + service)
                assert denied["status"] == "failed" and "受保护" in denied["result"]
        checks.append(f"protected_services_denied_{len(protected) * 3}_requests")

        with httpx.Client(timeout=5, trust_env=False) as fixture:
            for index in range(240):
                response = fixture.get(
                    "http://vey-smoke/",
                    params={"line": f"acceptance-{index:04}", "password": "fixture-secret-value"},
                )
                assert response.status_code == 200
        first = read({"name": "logs", "target": target})
        assert len(first["text"].splitlines()) == 100
        assert len(first["text"]) <= 10000 and "fixture-secret-value" not in first["text"]
        second = read({"name": "logs", "cursor": first["cursor"]})
        assert len(second["text"].splitlines()) == 100
        assert not (set(first["text"].splitlines()) & set(second["text"].splitlines()))
        large = read({"name": "logs", "target": target, "lines": 500})
        assert len(large["text"]) <= 10000 and large["truncated"] and large["cursor"]
        invalid = executor.post(
            "/read", json={"actor": actor(), "call": {"name": "shell", "command": "id"}}
        )
        assert invalid.status_code == 422
        checks.extend(
            [
                "real_logs_default100_redaction_pagination_character_cap",
                "executor_has_no_shell_tool",
            ]
        )

        for action, expected in (("停止", "exited"), ("启动", "running"), ("重启", "running")):
            message_id = uuid.uuid4().hex
            admission = submit(action + " " + target, message_id)
            assert submit(action + " " + target, message_id)["duplicate"]
            pending = wait(admission["task_id"])
            assert pending["status"] == "awaiting_confirmation"
            code = re.search(r"确认 ([A-F0-9]{8})", pending["result"])[1]
            confirmed = task("确认 " + code)
            assert confirmed["status"] == "succeeded", confirmed["result"]
            state = read({"name": "inspect", "target": target})["instances"][0]
            assert state["status"] == expected
            repeat = task("确认 " + code)
            assert repeat["status"] == "succeeded"
            # PostgreSQL JSONB may reorder object keys on the replay response.
            assert repeat["result"] == confirmed["result"]
            after_replay = read({"name": "inspect", "target": target})["instances"][0]
            assert all(after_replay[key] == state[key] for key in ("started_at", "finished_at"))
        checks.append("confirmed_stop_start_restart_and_duplicate_handling")

        pending = task("重启 " + target)
        code = re.search(r"确认 ([A-F0-9]{8})", pending["result"])[1]
        assert task("清空上下文")["status"] == "succeeded"
        assert task("确认 " + code)["status"] == "failed"
        checks.append("clear_context_revokes_confirmation")
        if not settings.model_key.get_secret_value():
            assert task("帮我分析测试服务为何不可访问")["status"] == "failed"
            checks.append("missing_model_key_fails_closed")
    print(json.dumps({"status": "passed", "checks": checks}, ensure_ascii=False))


if __name__ == "__main__":
    main()
