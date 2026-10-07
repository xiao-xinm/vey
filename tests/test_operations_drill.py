import copy
import importlib.util
import tarfile
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from vey.db import ChatSession, ExecutorSession, Grant, LogCursor, Outbox, Task
from vey.domain import utcnow

spec = importlib.util.spec_from_file_location(
    "operations_drill", Path(__file__).parents[1] / "scripts/operations_drill.py"
)
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)


def test_restore_identity_rejects_production_and_host_access():
    run_id = "a" * 32
    info = {
        "Id": "b" * 64,
        "Name": "/vey-restore-" + run_id[:12],
        "HostConfig": {"NetworkMode": "none", "PortBindings": {}},
        "Config": {"Labels": {"vey.restore-drill": run_id}},
        "Mounts": [],
    }
    drill.require_isolated(info, info["Id"], run_id)
    for field, value in [
        ("Id", "production"),
        ("Name", "/rag-postgres"),
        ("HostConfig", {"NetworkMode": "bridge"}),
        ("HostConfig", {"NetworkMode": "none", "PortBindings": {"5432": []}}),
        ("Config", {"Labels": {}}),
        ("Mounts", [{"Type": "bind", "Source": "/var/run/docker.sock"}]),
    ]:
        changed = copy.deepcopy(info)
        changed[field] = value
        with pytest.raises(ValueError):
            drill.require_isolated(changed, info["Id"], run_id)
    with pytest.raises(ValueError):
        drill.identifier("vey; DROP DATABASE vey")


def test_configuration_archive_is_complete_and_excludes_unrelated_files(tmp_path):
    project = tmp_path / "project"
    (project / "config").mkdir(parents=True)
    (project / "secrets").mkdir()
    (project / ".env").write_text("test-configuration")
    (project / "config/vey.toml").write_text("test-policy")
    (project / "secrets/test").write_text("fake-test-secret")
    (project / "unrelated.txt").write_text("not-selected")
    output = tmp_path / "backup.tar.gz"
    result = drill.archive_configuration(project, output)
    assert result["files"] == 3 and result["sha256"] == drill.sha256(output)
    with tarfile.open(output) as archive:
        assert sorted(archive.getnames()) == [".env", "config/vey.toml", "secrets/test"]
        assert archive.extractfile("secrets/test").read() == b"fake-test-secret"


@pytest.mark.postgres
def test_quarantine_removes_old_work_and_preserves_unknown_locks(db_factory, monkeypatch):
    import psycopg

    now = utcnow()
    statuses = ["queued", "control_queued", "running", "awaiting_confirmation", "succeeded"]
    with db_factory.begin() as db:
        for i, status in enumerate(statuses):
            task_id = f"{i:032x}"
            db.add(
                Task(
                    id=task_id,
                    message_id=task_id,
                    user_id="owner",
                    generation="g",
                    status=status,
                    text="old instruction",
                )
            )
        db.flush()
        db.add(Outbox(task_id=f"{0:032x}", user_id="owner", body="old confirmation"))
        for i, status in enumerate(["pending", "executing", "unknown", "succeeded"]):
            db.add(
                Grant(
                    code=f"{i:08x}",
                    task_id=f"{i:032x}",
                    user_id="owner",
                    generation="g",
                    target="fixture/web",
                    action="restart",
                    container_ids=["fixture"],
                    policy_hash="x",
                    status=status,
                    expires_at=now + timedelta(minutes=1),
                )
            )
        db.add(ChatSession(user_id="owner", generation="g", expires_at=now))
        db.add(ExecutorSession(user_id="owner", generation="g", started_at=now, expires_at=now))
        db.add(
            LogCursor(
                user_id="owner",
                generation="g",
                container_ids=[],
                target="fixture/web",
                payload={},
                expires_at=now,
            )
        )
    url = make_url(str(db_factory.kw["bind"].url.render_as_string(hide_password=False)))
    assert url.database.startswith("vey_test")

    def isolated_query(container, database, statement):
        assert container == "isolated-fixture" and database == "vey"
        with psycopg.connect(
            url.set(drivername="postgresql").render_as_string(hide_password=False), autocommit=True
        ) as connection:
            cursor = connection.execute(statement)
            if statement.startswith("SELECT"):
                return str(cursor.fetchone()[0])
            return ""

    monkeypatch.setattr(drill, "query", isolated_query)
    assert drill.quarantine("isolated-fixture")["replayable_records"] == 0
    with db_factory() as db:
        assert [db.get(Task, f"{i:032x}").status for i in range(5)] == ["unknown"] * 4 + [
            "succeeded"
        ]
        assert [db.get(Grant, f"{i:08x}").status for i in range(4)] == [
            "cancelled",
            "unknown",
            "unknown",
            "succeeded",
        ]
        assert all(t.text == "" for t in db.query(Task))
        assert db.query(Outbox).one().status == "expired"
