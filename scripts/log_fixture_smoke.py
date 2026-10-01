"""Verify retained log snapshots and multi-instance discovery using only test fixtures.

Run inside ops-executor during an active owner test session. Rotation writes only
synthetic logs to vey-acceptance/web. Multi mode requires two fixture replicas.
Neither mode changes the session clock or starts/stops containers.
"""

import argparse
import json

import httpx

from vey.config import Settings
from vey.db import ExecutorSession, database
from vey.docker_backend import DockerBackend
from vey.domain import Actor, ReadRequest, ToolCall
from vey.executor import Executor
from vey.logs import page_logs, sanitize_records

TARGET = "vey-acceptance/web"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["rotation", "multi"])
    args = parser.parse_args()
    settings = Settings()
    policy = settings.policy()
    service = policy.resolve(TARGET)
    engine, factory = database(settings.database_url.get_secret_value())
    backend = DockerBackend(settings)
    try:
        executor = Executor(factory, policy, backend, settings)
        with factory() as db:
            session = db.get(ExecutorSession, policy.owner_id)
            actor = Actor(
                user_id=session.user_id,
                generation=session.generation,
                started_at=session.started_at,
                expires_at=session.expires_at,
            )

        def read(**values):
            return executor.read(ReadRequest(actor=actor, call=ToolCall(name="logs", **values)))

        instances = backend.resolve(service)
        assert len(instances) == (1 if args.mode == "rotation" else 2)
        assert all(item["project"] == "vey-acceptance" for item in instances)
        with httpx.Client(timeout=5, trust_env=False) as client:
            if args.mode == "rotation":
                container = backend.client.containers.get(instances[0]["id"])
                config = container.attrs["HostConfig"]["LogConfig"]["Config"]
                assert config["max-size"] == "1m" and config["max-file"] == "1"
                original, _ = backend.logs([container.id], ToolCall(name="logs", target=TARGET))
                expected = page_logs(sanitize_records(original)[:-100]).text
                initial = read(target=TARGET)
                assert initial["cursor"] and expected
                for index in range(300):
                    response = client.get(
                        "http://vey-smoke/", params={"line": f"rotation-{index:04}-" + "x" * 6000}
                    )
                    response.raise_for_status()
                older = read(cursor=initial["cursor"])
                assert older["text"] == expected
                retained, _ = backend.logs([container.id], ToolCall(name="logs", target=TARGET))
                assert 0 < len(retained) < 300 and "rotation-0299" in retained[-1]
                latest = read(target=TARGET, lines=1)
                assert "rotation-0299" in latest["text"] and len(latest["text"]) <= 10000
                print(
                    json.dumps(
                        {
                            "check": "real_rotation_keeps_cached_page",
                            "retained_lines": len(retained),
                        }
                    )
                )
            else:
                for instance in instances:
                    container = backend.client.containers.get(instance["id"])
                    networks = container.attrs["NetworkSettings"]["Networks"]
                    assert len(networks) == 1
                    ip = next(iter(networks.values()))["IPAddress"]
                    for index in range(20):
                        response = client.get(
                            f"http://{ip}/",
                            params={"line": f"multi-fixture-{instance['id'][:12]}-{index:03}"},
                        )
                        response.raise_for_status()
                result = read(target=TARGET, keyword="multi-fixture")
                assert len(result["text"].splitlines()) == 40
                for instance in instances:
                    assert f"[{instance['id'][:12]}]" in result["text"]
                timestamps = [line.split(" ", 1)[0] for line in result["text"].splitlines()]
                assert timestamps == sorted(timestamps)
                print(
                    json.dumps({"check": "real_multi_instance_logs", "instances": 2, "lines": 40})
                )
    finally:
        backend.close()
        engine.dispose()


if __name__ == "__main__":
    main()
