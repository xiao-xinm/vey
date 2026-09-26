"""Verify real model calls through the running core worker without opening debug HTTP.

Run via `docker compose exec -T agent-core python - < scripts/model_smoke.py`.
Uses the configured owner's session, clears context and makes billable model requests.
Only readonly scenarios are submitted; no operation confirmations are sent.
"""

import json
import time
import uuid

from sqlalchemy import select

from vey.config import Settings
from vey.core import Core
from vey.db import Event, database


def main():
    settings = Settings()
    if not settings.model_key.get_secret_value():
        raise SystemExit("Configure the model secret file first")
    engine, factory = database(settings.database_url.get_secret_value())
    # This process only admits tasks and reads audit records; the existing core
    # worker performs routing, model requests and executor calls.
    admission = Core(factory, settings.policy(), None, None)

    def submit(message, expect_model=True):
        receipt = admission.accept(
            "model-smoke:" + uuid.uuid4().hex, settings.policy().owner_id, message
        )
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            task = admission.task(receipt["task_id"])
            if task["status"] not in {"queued", "control_queued", "running"}:
                with factory() as db:
                    events = list(
                        db.scalars(
                            select(Event).where(Event.task_id == task["id"]).order_by(Event.id)
                        )
                    )
                metrics = [event.data for event in events if event.kind == "model"]
                # Final model metrics are persisted just after the task result.
                if not expect_model or metrics or task["status"] != "succeeded":
                    return task, events
            time.sleep(0.25)
        raise RuntimeError("Model acceptance task exceeded its deadline")

    cases = [
        ("host_resources", "请只查看这台服务器当前的 CPU 和内存使用情况。", "query", {"system"}),
        (
            "evidence_based_diagnosis",
            "请排查运维助手这个服务当前是否正常，先查容器状态，再读取近期日志，结合真实证据说明结论和仍不确定的事项。只进行只读排查。",
            "diagnose",
            {"inspect", "logs"},
        ),
    ]
    try:
        assert submit("清空上下文", expect_model=False)[0]["status"] == "succeeded"
        for case, message, expected_intent, expected_tools in cases:
            task, events = submit(message)
            metrics = [e.data for e in events if e.kind == "model"]
            tools = [e.data.get("call", {}).get("name") for e in events if e.kind == "tool"]
            intents = [e.data.get("kind") for e in events if e.kind == "intent"]
            errors = [e.data for e in events if e.kind == "error"]
            print(
                json.dumps(
                    {
                        "case": case,
                        "task_id": task["id"],
                        "status": task["status"],
                        "intents": intents,
                        "tools": tools,
                        "model_calls": metrics,
                        "errors": errors,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            assert not any(e.kind in {"operation_prepared", "operation"} for e in events)
            assert task["status"] == "succeeded" and intents == [expected_intent]
            assert metrics and all(m["status"] == "ok" for m in metrics)
            assert 1 <= len(tools) <= 5 and expected_tools.issubset(tools)
            if case == "evidence_based_diagnosis":
                assert "E1" in task["result"] and "E2" in task["result"]
        print(
            json.dumps(
                {
                    "status": "passed",
                    "cases": len(cases),
                    "model": settings.model_name,
                    "public_debug_enabled": settings.debug_enabled,
                }
            ),
            flush=True,
        )
    finally:
        submit("清空上下文", expect_model=False)
        engine.dispose()


if __name__ == "__main__":
    main()
