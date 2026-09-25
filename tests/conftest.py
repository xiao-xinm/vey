import os
from datetime import timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from vey.config import Policy, Settings
from vey.core import Core
from vey.db import Base, database
from vey.domain import Actor, Intent, NextStep, ToolCall, utcnow
from vey.executor import Executor


@pytest.fixture
def policy():
    return Policy(
        owner_id="owner",
        self_project="vey",
        protected_projects=["vey", "infra"],
        database_protection_acknowledged=True,
        services=[
            {"key": "blog/web", "aliases": ["博客"]},
            {"key": "infra/postgres", "aliases": ["数据库"], "protected": True},
            {"key": "vey/agent-core", "aliases": ["自己"], "protected": True},
        ],
    )


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://test@localhost/vey_test",
        executor_token="x" * 40,
        debug_token="y" * 40,
        debug_enabled=True,
        worker_enabled=False,
        model_key="fake-test-key",
        config=tmp_path / "unused.toml",
    )


@pytest.fixture
def actor():
    now = utcnow()
    return Actor(
        user_id="owner",
        generation="generation-1",
        started_at=now,
        expires_at=now + timedelta(minutes=15),
    )


@pytest.fixture(scope="session")
def migrated_database():
    url = os.environ.get("VEY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set VEY_TEST_DATABASE_URL to an isolated PostgreSQL vey_test* database")
    engine, factory = database(url)
    with engine.begin() as connection:
        name = connection.scalar(text("SELECT current_database()"))
        if not name.startswith("vey_test"):
            pytest.fail("Refusing destructive test setup outside vey_test* database")
        connection.execute(text("DROP SCHEMA IF EXISTS vey_core CASCADE"))
        connection.execute(text("DROP SCHEMA IF EXISTS vey_exec CASCADE"))
        connection.execute(text("DROP TABLE IF EXISTS public.alembic_version"))
    previous = os.environ.get("VEY_MIGRATION_DATABASE_URL")
    os.environ["VEY_MIGRATION_DATABASE_URL"] = url
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        if previous is None:
            os.environ.pop("VEY_MIGRATION_DATABASE_URL", None)
        else:
            os.environ["VEY_MIGRATION_DATABASE_URL"] = previous
    yield engine, factory
    engine.dispose()


@pytest.fixture
def db_factory(migrated_database):
    engine, factory = migrated_database
    with engine.begin() as connection:
        names = ", ".join(f"{table.schema}.{table.name}" for table in Base.metadata.sorted_tables)
        connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    yield factory


class FakeDocker:
    def __init__(self):
        self.instances = {
            "blog/web": [
                {
                    "id": "a" * 64,
                    "name": "blog-web",
                    "status": "running",
                    "health": "not_configured",
                    "project": "blog",
                }
            ],
            "infra/postgres": [{"id": "b" * 64, "status": "running", "project": "infra"}],
            "vey/agent-core": [{"id": "c" * 64, "status": "running", "project": "vey"}],
        }
        self.actions = []
        self.records = [f"line-{i}" for i in range(300)]
        self.fail = False

    def resolve(self, service):
        return [dict(c) for c in self.instances.get(service.key, [])]

    def inspect(self, container_id):
        return next(
            dict(c) for group in self.instances.values() for c in group if c["id"] == container_id
        )

    def mutate(self, container_id, action):
        self.actions.append((container_id, action))
        if self.fail:
            raise TimeoutError("simulated response lost after request accepted")
        for group in self.instances.values():
            for item in group:
                if item["id"] == container_id:
                    item["status"] = "exited" if action == "stop" else "running"

    def logs(self, ids, call):
        return self.records, False

    def stats(self, container_id):
        return {"id": container_id, "memory_bytes": 1000}

    def health(self, service):
        return {"status": "not_configured"}


@pytest.fixture
def backend():
    return FakeDocker()


@pytest.fixture
def executor(db_factory, policy, backend, settings, actor):
    service = Executor(db_factory, policy, backend, settings)
    service.sync_actor(actor)
    return service


class LocalExecutorClient:
    def __init__(self, executor):
        self.service = executor

    async def sync_actor(self, actor):
        return self.service.sync_actor(actor)

    async def read(self, request):
        return self.service.read(request)

    async def prepare(self, request):
        return self.service.prepare(request)

    async def confirm(self, request):
        return self.service.confirm(request)

    async def cancel(self, request):
        return self.service.cancel(request)

    async def status(self, actor, task_id):
        return self.service.operation_status(actor, task_id)


class FakeModel:
    metrics = []

    async def route(self, message, context, catalog):
        return Intent(kind="diagnose", target="blog/web")

    async def next_step(self, message, evidence, target, catalog):
        return NextStep(tool=ToolCall(name="logs", target=target, keyword=str(len(evidence))))


@pytest.fixture
def core(db_factory, policy, executor):
    return Core(db_factory, policy, LocalExecutorClient(executor), FakeModel())
