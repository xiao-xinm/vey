"""Observe a real 15-minute owner-session timeout without modifying timestamps.

Run inside agent-core while no user sends new messages. The temporary Docker fixture
must still exist. Creates one readonly task, sends no WeCom messages, and waits for
the normal background cleanup. A user message invalidates the test, not the user.
"""

import json
import time
import uuid
from datetime import timedelta

import httpx

from vey.config import Settings
from vey.core import Core
from vey.db import ChatSession, Task, database
from vey.domain import Actor, VeyError, utcnow


def main():
    settings = Settings()
    policy = settings.policy()
    assert policy.resolve("vey-acceptance/web").key == "vey-acceptance/web"
    engine, factory = database(settings.database_url.get_secret_value())
    core = Core(factory, policy, None, None)
    try:
        receipt = core.accept(
            "idle-test:" + uuid.uuid4().hex, policy.owner_id, "日志 vey-acceptance/web"
        )
        deadline = time.monotonic() + 30
        while core.task(receipt["task_id"])["status"] in {"queued", "running"}:
            assert time.monotonic() < deadline
            time.sleep(0.25)
        assert core.task(receipt["task_id"])["status"] == "succeeded"
        with factory() as db:
            session = db.get(ChatSession, policy.owner_id)
            assert session.context.get("target") == "vey-acceptance/web"
            generation, expires = session.generation, session.expires_at
            actor = Actor(
                user_id=session.user_id,
                generation=session.generation,
                started_at=session.started_at,
                expires_at=session.expires_at,
            )
        print(json.dumps({"stage": "idle_wait", "expires_at": expires.isoformat()}), flush=True)
        next_report = time.monotonic() + 60
        while utcnow() < expires + timedelta(seconds=40):
            with factory() as db:
                current = db.get(ChatSession, policy.owner_id)
                assert current.generation == generation and current.expires_at == expires, (
                    "Session changed by new input; idle test must be rerun"
                )
                if utcnow() >= expires and not current.context:
                    task = db.get(Task, receipt["task_id"])
                    assert task.text == "" and task.result
                    break
            if time.monotonic() >= next_report:
                print(
                    json.dumps(
                        {
                            "idle_seconds_remaining": max(
                                0, int((expires - utcnow()).total_seconds())
                            )
                        }
                    ),
                    flush=True,
                )
                next_report = time.monotonic() + 60
            time.sleep(5)
        else:
            raise AssertionError("Background session cleanup did not complete")
        try:
            core._input(receipt["task_id"])
        except VeyError as exc:
            assert exc.code == "expired_session"
        else:
            raise AssertionError("Expired task context was still accepted")
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=settings.executor_socket),
            base_url="http://executor",
            headers={"Authorization": "Bearer " + settings.executor_token.get_secret_value()},
            timeout=10,
        ) as client:
            response = client.post(
                "/read",
                json={
                    "policy_revision": client.get("/policy").json()["revision"],
                    "actor": actor.model_dump(mode="json"),
                    "call": {"name": "system"},
                },
            )
            assert response.status_code == 403
        print(
            json.dumps(
                {
                    "status": "passed",
                    "check": "real_15_minute_idle_expiry",
                    "task": receipt["task_id"],
                    "observed_at": utcnow().isoformat(),
                    "expires_at": expires.isoformat(),
                    "cleanup_delay_seconds_upper_bound": (utcnow() - expires).total_seconds(),
                }
            ),
            flush=True,
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
