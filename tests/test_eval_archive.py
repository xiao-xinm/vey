import json
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from vey.evaluation.archive import archive_report, connect_archive, initialize

pytestmark = pytest.mark.postgres


@pytest.fixture
def archive_db(migrated_database):
    url = make_url(os.environ["VEY_TEST_DATABASE_URL"])
    name = "vey_test_eval_" + uuid.uuid4().hex
    connect = dict(host=url.host, port=url.port, user=url.username, password=url.password)
    with psycopg.connect(**connect, dbname="postgres", autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            with connect_archive(
                url.set(database=name).render_as_string(hide_password=False)
            ) as db:
                initialize(db)
                yield db
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_real_archive_idempotence_immutable_report_and_atomicity(archive_db):
    report = json.loads(
        Path("evals/reports/2026-10-04-baseline/hybrid-test/report.json").read_text(
            encoding="utf-8"
        )
    )
    assert archive_report(archive_db, report)["inserted"]
    assert not archive_report(archive_db, report)["inserted"]
    assert archive_db.execute("SELECT count(*) FROM vey_eval.samples").fetchone()[0] == 20
    report["status"] = "interrupted"
    with pytest.raises(ValueError, match="immutable"):
        archive_report(archive_db, report)
    report["run_id"] = uuid.uuid4().hex
    report["results"][1]["case_id"] = report["results"][0]["case_id"]
    with pytest.raises(ValueError, match="Duplicate"):
        archive_report(archive_db, report)
    assert archive_db.execute("SELECT count(*) FROM vey_eval.runs").fetchone()[0] == 1


def test_archive_role_cannot_change_or_delete_history(archive_db):
    role = "vey_test_eval_writer_" + uuid.uuid4().hex[:8]
    # Role and grants roll back together; no reusable credential is generated.
    with archive_db.transaction(force_rollback=True):
        archive_db.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE").format(
                sql.Identifier(role)
            )
        )
        initialize(archive_db, role)
        archive_db.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
        report = json.loads(
            Path("evals/reports/2026-10-04-baseline/model-test/report.json").read_text(
                encoding="utf-8"
            )
        )
        assert archive_report(archive_db, report)["inserted"]
        assert not archive_report(archive_db, report)["inserted"]
        for statement in (
            "DELETE FROM vey_eval.runs",
            "UPDATE vey_eval.runs SET scope='fake'",
            "CREATE TABLE vey_eval.forbidden (id int)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), archive_db.transaction():
                archive_db.execute(statement)


def test_refuses_runtime_database_and_redacts_before_archive(migrated_database, archive_db):
    with pytest.raises(ValueError, match="Archive requires"):
        connect_archive(os.environ["VEY_TEST_DATABASE_URL"])
    report = json.loads(
        Path("evals/reports/2026-10-04-baseline/model-test/report.json").read_text(encoding="utf-8")
    )
    report["note"] = "password=never-store-this"
    archive_report(archive_db, report)
    stored = archive_db.execute("SELECT report FROM vey_eval.runs").fetchone()[0]
    assert "never-store-this" not in json.dumps(stored)


def test_dedicated_reader_permissions_pagination_and_failure_filter(archive_db):
    from vey.evaluation.reader import EvaluationReader, install_reader

    role = "vey_test_eval_reader_" + uuid.uuid4().hex[:8]
    password = uuid.uuid4().hex + uuid.uuid4().hex
    report = json.loads(
        Path("evals/reports/2026-10-06-diagnosis-fix/known-regression/report.json").read_text(
            encoding="utf-8"
        )
    )
    archive_report(archive_db, report)
    second = dict(report, run_id=uuid.uuid4().hex)
    archive_report(archive_db, second)
    install_reader(archive_db, password, role)
    archive_db.commit()
    url = make_url(os.environ["VEY_TEST_DATABASE_URL"]).set(
        database=archive_db.info.dbname, username=role, password=password
    )
    try:
        reader = EvaluationReader(url.render_as_string(hide_password=False))
        first = reader.runs(limit=1)
        assert len(first["items"]) == 1 and first["next"]
        older = reader.runs(before=first["next"], limit=1)
        assert older["items"][0]["run_id"] != first["items"][0]["run_id"]
        assert first["items"][0]["metrics"]["passed"] == 5
        assert first["items"][0]["metrics"]["estimated_cost"] is None
        failed = reader.detail(report["run_id"], status="failed")
        assert [s["case_id"] for s in failed["samples"]] == ["permission-insufficient"]
        assert failed["samples"][0]["result"]["failures"] == ["unsupported_causal_claim"]
        assert failed["run"]["scoring_label"] == "trajectory-v2-temporal-polarity"
        with connect_archive(url.render_as_string(hide_password=False)) as db:
            assert db.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
            db.execute("SET default_transaction_read_only=off")
            db.commit()
            for statement in (
                "DELETE FROM vey_eval.runs",
                "UPDATE vey_eval.samples SET status='passed'",
                "INSERT INTO vey_eval.runs SELECT * FROM vey_eval.runs WHERE false",
                "CREATE TABLE vey_eval.bad(id int)",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
                    db.execute(statement)
    finally:
        archive_db.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        archive_db.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        archive_db.commit()
