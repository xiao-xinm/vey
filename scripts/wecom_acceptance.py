"""Exercise the deployed callback, worker, Docker fixture and real WeCom delivery.

Run inside agent-core. Requires an explicitly created vey-acceptance/web fixture.
Incoming messages are signed test callbacks, NOT messages typed in the WeCom client.
Representative replies go to the configured owner via the real application API.
Clears the owner's session. Never modifies any service except the fixed fixture.
"""

import argparse
import json
import re
import time
import uuid
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx
from sqlalchemy import select

from vey.config import Settings
from vey.core import Core
from vey.db import ChatSession, Event, Outbox, database
from vey.domain import Actor
from vey.wecom import WeComCipher

TARGET = "vey-acceptance/web"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--callback-url", required=True)
    args = parser.parse_args()
    assert args.callback_url.startswith("https://")
    settings = Settings()
    policy = settings.policy()
    assert policy.resolve(TARGET).key == TARGET and not settings.debug_enabled
    engine, factory = database(settings.database_url.get_secret_value())
    admission = Core(factory, policy, None, None)
    cipher = WeComCipher(
        settings.wecom_token.get_secret_value(),
        settings.wecom_aes_key.get_secret_value(),
        settings.wecom_corp_id,
    )
    checks, tasks = [], []

    def record(name, **details):
        checks.append(name)
        print(json.dumps({"check": name, **details}, ensure_ascii=False), flush=True)

    with (
        httpx.Client(timeout=15, trust_env=False) as callback,
        httpx.Client(
            transport=httpx.HTTPTransport(uds=settings.executor_socket),
            base_url="http://executor",
            headers={"Authorization": "Bearer " + settings.executor_token.get_secret_value()},
            timeout=35,
            trust_env=False,
        ) as executor,
    ):

        def post(message, message_id):
            xml = Element("xml")
            for key, value in {
                "ToUserName": settings.wecom_corp_id,
                "FromUserName": policy.owner_id,
                "AgentID": str(settings.wecom_agent_id),
                "MsgType": "text",
                "MsgId": message_id,
                "Content": message,
            }.items():
                SubElement(xml, key).text = value
            encrypted = cipher.encrypt(tostring(xml, encoding="utf-8"))
            timestamp, nonce = str(int(time.time())), uuid.uuid4().hex
            response = callback.post(
                args.callback_url,
                params={
                    "timestamp": timestamp,
                    "nonce": nonce,
                    "msg_signature": cipher.signature(timestamp, nonce, encrypted),
                },
                content=f"<xml><Encrypt>{encrypted}</Encrypt></xml>",
            )
            assert response.status_code == 200 and response.text == ""

        def submit(message, public=False, duplicate=False):
            message_id = "acceptance:" + uuid.uuid4().hex
            if public:
                post(message, message_id)
                if duplicate:
                    post(message, message_id)
                from vey.db import Task

                with factory() as db:
                    task_id = db.scalar(
                        select(Task.id).where(Task.message_id == "wecom:" + message_id)
                    )
                assert task_id
            else:
                task_id = admission.accept(message_id, policy.owner_id, message)["task_id"]
            tasks.append(task_id)
            deadline = time.monotonic() + 80
            while time.monotonic() < deadline:
                task = admission.task(task_id)
                if task["status"] not in {"queued", "control_queued", "running"}:
                    if not public:
                        return task
                    with factory() as db:
                        rows = list(db.scalars(select(Outbox).where(Outbox.task_id == task_id)))
                        assert not any(row.status == "failed" for row in rows), "Delivery failed"
                        if len(rows) == 2 and all(row.status == "sent" for row in rows):
                            result = next(row for row in rows if row.kind == "result")
                            task["delivery_body"] = result.body
                            task["sent_parts"] = result.sent_parts
                            return task
                time.sleep(0.25)
            raise AssertionError("Task or WeCom delivery exceeded deadline")

        def actor():
            with factory() as db:
                session = db.get(ChatSession, policy.owner_id)
                return Actor(
                    user_id=session.user_id,
                    generation=session.generation,
                    started_at=session.started_at,
                    expires_at=session.expires_at,
                ).model_dump(mode="json")

        def read(call):
            response = executor.post("/read", json={"actor": actor(), "call": call})
            response.raise_for_status()
            return response.json()

        def state():
            instances = read({"name": "inspect", "target": TARGET})["instances"]
            assert len(instances) == 1 and instances[0]["project"] == "vey-acceptance"
            return instances[0]

        def prepare(public=False):
            task = submit("重启 " + TARGET, public=public, duplicate=public)
            assert task["status"] == "awaiting_confirmation"
            code = re.search(r"确认 ([A-F0-9]{8})", task["result"])[1]
            return task, code

        def unchanged(before):
            after = state()
            assert all(after[key] == before[key] for key in ("id", "started_at", "finished_at"))

        try:
            assert submit("清空上下文")["status"] == "succeeded"
            assert state()["status"] == "running"
            # Only the named fixture receives these requests; the dummy secret is deliberate.
            for index in range(240):
                response = callback.get(
                    "http://vey-smoke/",
                    params={"line": f"acceptance-{index:04}", "password": "fixture-secret"},
                )
                assert response.status_code == 200
            first = submit("日志 " + TARGET, public=True, duplicate=True)
            second = submit("下一页", public=True)
            first_lines = set(re.findall(r"acceptance-\d{4}", first["delivery_body"]))
            second_lines = set(re.findall(r"acceptance-\d{4}", second["delivery_body"]))
            assert len(first_lines) == len(second_lines) == 100
            assert not first_lines & second_lines
            assert "fixture-secret" not in first["delivery_body"]
            assert first["sent_parts"] > 1
            record("public_log_pagination_redaction_delivery", parts=first["sent_parts"])

            before = state()
            protected_checks = 0
            for service in policy.services:
                if service.protected or service.key.split("/")[0] in policy.protected_projects:
                    for action in ("启动", "停止", "重启"):
                        denied = submit(action + " " + service.key)
                        assert denied["status"] == "failed" and "受保护" in denied["result"]
                        protected_checks += 1
            unchanged(before)
            record("protected_services_rejected", requests=protected_checks)

            for action, expected in (("停止", "exited"), ("启动", "running")):
                task = submit(action + " " + TARGET)
                assert task["status"] == "awaiting_confirmation"
                code = re.search(r"确认 ([A-F0-9]{8})", task["result"])[1]
                assert submit("确认 " + code)["status"] == "succeeded"
                assert state()["status"] == expected
            prepared, code = prepare(public=True)
            before = state()
            assert submit("确认 " + code, public=True, duplicate=True)["status"] == "succeeded"
            after = state()
            assert after["status"] == "running" and after["started_at"] != before["started_at"]
            assert submit("确认 " + code)["status"] == "succeeded"
            unchanged(after)
            record(
                "confirmed_stop_start_public_restart_no_duplicate_execution", task=prepared["id"]
            )

            before = state()
            _, code = prepare()
            assert submit("取消 " + code)["status"] == "cancelled"
            assert submit("确认 " + code)["status"] == "cancelled"
            unchanged(before)
            record("cancel_prevents_mutation")

            expiring, expiring_code = prepare()
            expiry_started = time.monotonic()
            diagnosis = submit(
                "请只读排查 vey-acceptance/web 测试服务是否正常，检查状态、近期日志和业务健康，"
                "引用已有证据说明结论，不执行修改操作。",
                public=True,
            )
            with factory() as db:
                events = list(db.scalars(select(Event).where(Event.task_id == diagnosis["id"])))
            tools = [e for e in events if e.kind == "tool"]
            assert diagnosis["status"] == "succeeded" and 1 <= len(tools) <= 5
            assert not any(e.kind in {"operation_prepared", "operation"} for e in events)
            assert "E1" in diagnosis["result"]
            record("real_model_public_diagnosis", tools=len(tools), task=diagnosis["id"])
            unchanged(before)
            while time.monotonic() - expiry_started < 125:
                time.sleep(min(10, 125 - (time.monotonic() - expiry_started)))
            expired = submit("确认 " + expiring_code)
            assert expired["status"] in {"expired", "failed"}
            unchanged(before)
            record("real_two_minute_confirmation_expiry", task=expiring["id"])

            _, code = prepare()
            page = read({"name": "logs", "target": TARGET})
            old_cursor = page["cursor"]
            assert old_cursor
            assert submit("清空上下文")["status"] == "succeeded"
            assert submit("确认 " + code)["status"] == "failed"
            invalid = executor.post(
                "/read", json={"actor": actor(), "call": {"name": "logs", "cursor": old_cursor}}
            )
            assert invalid.status_code == 409
            unchanged(before)
            record("clear_context_revokes_confirmation_and_cursor")
            print(
                json.dumps(
                    {"status": "passed", "checks": checks, "task_ids": tasks}, ensure_ascii=False
                ),
                flush=True,
            )
        finally:
            submit("清空上下文")
            engine.dispose()


if __name__ == "__main__":
    main()
