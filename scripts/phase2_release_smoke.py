"""Verify the running release using internal task admission, without WeCom sends.

Only vey-acceptance/web can be restarted. Run inside the production core after
creating the explicit fixture. The real worker handles every accepted task.
"""

import argparse
import json
import re
import time
import uuid
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from vey.config import Settings
from vey.core import Core
from vey.db import ChatSession, Event, database
from vey.domain import Actor
from vey.wecom import WeComCipher


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-base-url", required=True)
    args = parser.parse_args()
    parsed = urlsplit(args.public_base_url)
    assert (
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.query
        and not parsed.fragment
        and parsed.path in {"", "/"}
    )
    public_base = args.public_base_url.rstrip("/")
    settings = Settings()
    policy = settings.policy()
    assert not settings.debug_enabled
    target = policy.resolve("vey-acceptance/web").key
    engine, factory = database(settings.database_url.get_secret_value())
    core = Core(factory, policy, None, None)
    results = []

    def task(message):
        receipt = core.accept("phase2-release:" + uuid.uuid4().hex, policy.owner_id, message)
        deadline = time.monotonic() + 80
        while time.monotonic() < deadline:
            value = core.task(receipt["task_id"])
            if value["status"] not in {"queued", "control_queued", "running"}:
                return value
            time.sleep(0.2)
        raise RuntimeError("Release task exceeded deadline")

    def events(task_id):
        with factory() as db:
            return list(db.scalars(select(Event).where(Event.task_id == task_id)))

    def record(name, value):
        result = {"check": name, "task_id": value["id"], "status": value["status"]}
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)

    with httpx.Client(
        transport=httpx.HTTPTransport(uds=settings.executor_socket),
        base_url="http://executor",
        timeout=35,
        trust_env=False,
    ) as executor:
        executor.headers["Authorization"] = "Bearer " + settings.executor_token.get_secret_value()

        def state():
            with factory() as db:
                session = db.get(ChatSession, policy.owner_id)
                actor = Actor(
                    user_id=session.user_id,
                    generation=session.generation,
                    started_at=session.started_at,
                    expires_at=session.expires_at,
                )
            response = executor.post(
                "/read",
                json={
                    "actor": actor.model_dump(mode="json"),
                    "call": {"name": "inspect", "target": target},
                    "policy_revision": executor.get("/policy").json()["revision"],
                },
            )
            response.raise_for_status()
            instances = response.json()["instances"]
            assert len(instances) == 1 and instances[0]["project"] == "vey-acceptance"
            return instances[0]

        try:
            assert task("清空上下文")["status"] == "succeeded"
            before = state()
            assert before["status"] == "running" and "oom_killed" in before
            for message in ("服务器概况", "资源排行", "日志 " + target):
                value = task(message)
                assert value["status"] == "succeeded"
                record("query:" + message, value)
            value = task(
                "请只读排查 vey-acceptance/web 测试服务是否正常，先查状态、近期日志和业务健康，再说明证据支持什么以及还不确定什么。不执行任何修改。"
            )
            audit = events(value["id"])
            diagnostic = next(e.data for e in audit if e.kind == "diagnosis")
            assert value["status"] == "succeeded" and diagnostic["stop_reason"] == "concluded"
            assert diagnostic["diagnosis"]["uncertainty"]
            assert 1 <= diagnostic["tool_calls"] <= 5
            assert not any(e.kind == "operation_prepared" for e in audit)
            record("structured_readonly_diagnosis", value)
            value = task("请把测试服务和运维助手两个服务都重启一下，先发确认。")
            audit = events(value["id"])
            assert any(e.kind == "intent" and e.data["kind"] == "clarify" for e in audit)
            assert not any(e.kind == "operation_prepared" for e in audit)
            assert state()["started_at"] == before["started_at"]
            record("multitarget_requires_clarification", value)
            protected = task("重启 运维助手")
            assert protected["status"] == "failed"
            assert not any(e.kind == "operation_prepared" for e in events(protected["id"]))
            record("self_service_protected", protected)
            prepared = task("重启 " + target)
            assert prepared["status"] == "awaiting_confirmation"
            assert state()["started_at"] == before["started_at"]
            code = re.search(r"确认 ([A-F0-9]{8})", prepared["result"])[1]
            done = task("确认 " + code)
            assert done["status"] == "succeeded"
            after = state()
            assert after["status"] == "running" and after["started_at"] != before["started_at"]
            duplicate = task("确认 " + code)
            assert duplicate["status"] == "succeeded"
            assert state()["started_at"] == after["started_at"]
            record("fixture_confirmation_and_no_replay", done)
            cipher = WeComCipher(
                settings.wecom_token.get_secret_value(),
                settings.wecom_aes_key.get_secret_value(),
                settings.wecom_corp_id,
            )
            challenge = "phase2-release-" + uuid.uuid4().hex
            encrypted = cipher.encrypt(challenge.encode())
            timestamp, nonce = str(int(time.time())), uuid.uuid4().hex
            with httpx.Client(timeout=15, trust_env=False) as public:
                response = public.get(
                    public_base + "/wecom/callback",
                    params={
                        "timestamp": timestamp,
                        "nonce": nonce,
                        "msg_signature": cipher.signature(timestamp, nonce, encrypted),
                        "echostr": encrypted,
                    },
                )
                assert response.status_code == 200 and response.text == challenge
                for path in ("/debug", "/health/ready", "/openapi.json"):
                    assert public.get(public_base + path).status_code == 404
                assert public.get(public_base + "/minio/").status_code == 200
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "checks": results,
                        "public_callback_challenge": True,
                        "private_paths_hidden": True,
                        "minio_https": True,
                        "wecom_messages_sent": 0,
                        "only_modified_target": target,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        finally:
            task("清空上下文")
            engine.dispose()


if __name__ == "__main__":
    main()
