import os
import uuid

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.postgres


def test_upgrade_preserves_existing_outbox_data(migrated_database, monkeypatch):
    url = make_url(os.environ["VEY_TEST_DATABASE_URL"])
    assert url.database.startswith("vey_test")
    name = "vey_test_upgrade_" + uuid.uuid4().hex
    connect = dict(host=url.host, port=url.port, user=url.username, password=url.password)
    with psycopg.connect(**connect, dbname="postgres", autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            monkeypatch.setenv(
                "VEY_MIGRATION_DATABASE_URL",
                url.set(database=name).render_as_string(hide_password=False),
            )
            command.upgrade(Config("alembic.ini"), "0001")
            with psycopg.connect(**connect, dbname=name) as db:
                db.execute(
                    "INSERT INTO vey_core.tasks VALUES "
                    "('task', 'message', 'owner', 'generation', 'query', 'wecom', "
                    "'succeeded', 'result', now(), now(), NULL)"
                )
                db.execute(
                    "INSERT INTO vey_core.outbox VALUES "
                    "('outbox', 'task', 'result', 'owner', 'retained body', "
                    "'pending', 2, now(), now())"
                )
            command.upgrade(Config("alembic.ini"), "head")
            with psycopg.connect(**connect, dbname=name) as db:
                assert db.execute(
                    "SELECT body, status, attempts, sent_parts FROM vey_core.outbox"
                ).fetchone() == ("retained body", "pending", 2, 0)
                assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0002",)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
