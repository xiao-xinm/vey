import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, Header
from sqlalchemy import text

from vey.api_common import install_errors
from vey.config import Settings
from vey.db import database
from vey.docker_backend import DockerBackend
from vey.domain import Actor, ConfirmRequest, PrepareRequest, ReadRequest, VeyError, digest
from vey.executor import Executor
from vey.policy_registry import Change, PolicyRegistry, Publication
from vey.security import verify_token


def create_app(settings=None, executor=None, registry=None):
    settings = settings or Settings()
    engine = None
    if executor is None:
        engine, factory = database(settings.database_url.get_secret_value())
        executor = Executor(factory, settings.policy(), DockerBackend(settings), settings)
    policy_engine = None
    management_token = ""
    if settings.policy_management_enabled:
        if settings.policy_management_token_file is None:
            raise ValueError("Management token file required")
        management_token = settings.policy_management_token_file.read_text().strip()
        if management_token == settings.executor_token.get_secret_value():
            raise ValueError("Management token must differ from operation token")
        if registry is None:
            policy_engine, policy_factory = database(settings.database_url.get_secret_value())
            registry = PolicyRegistry(
                policy_factory, executor.policy, executor.backend, management_token
            )

    @asynccontextmanager
    async def lifespan(app):
        if registry:
            await asyncio.to_thread(registry.initialize)
        await asyncio.to_thread(executor.recover)

        async def maintain():
            while True:
                with suppress(Exception):
                    await asyncio.to_thread(executor.cleanup)
                await asyncio.sleep(30)

        maintenance = asyncio.create_task(maintain())
        yield
        maintenance.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance
        if engine:
            executor.backend.close()
            engine.dispose()
        if policy_engine:
            policy_engine.dispose()

    app = FastAPI(
        title="Vey executor", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    install_errors(app)

    def authenticated(authorization: str | None = Header(default=None)):
        verify_token(authorization, settings.executor_token.get_secret_value())

    guard = [Depends(authenticated)]

    def managed(authorization: str | None = Header(default=None)):
        if not registry:
            raise VeyError("not_found", "配置管理未启用", 404)
        verify_token(authorization, management_token)

    def invoke(method, request):
        if not registry:
            return getattr(executor, method)(request)
        with registry.guard() as (policy, revision):
            if (
                isinstance(request, (ReadRequest, PrepareRequest))
                and request.policy_revision != revision
            ):
                raise VeyError("stale_policy", "服务配置已变化，请重新发起查询或操作", 409)
            current = Executor(executor.factory, policy, executor.backend, executor.settings)
            current.policy_hash = digest({"policy": policy.model_dump(), "revision": revision})
            return getattr(current, method)(request)

    @app.get("/policy", dependencies=guard)
    def policy():
        return (
            registry.catalog()
            if registry
            else {"policy": executor.policy.model_dump(), "revision": executor.policy_hash}
        )

    @app.get("/management/config", dependencies=[Depends(managed)])
    def configuration():
        return registry.listing()

    @app.post("/management/config/preview", dependencies=[Depends(managed)])
    def preview(change: Change):
        return registry.preview(change)

    @app.post("/management/config/publish", dependencies=[Depends(managed)])
    def publish(change: Publication):
        return registry.publish(change)

    @app.get("/health", dependencies=guard)
    def health():
        with executor.factory() as db:
            db.execute(text("SELECT code FROM vey_exec.grants LIMIT 0"))
        return {"status": "ok"}

    @app.post("/session", dependencies=guard)
    def session(actor: Actor):
        return executor.sync_actor(actor)

    @app.post("/read", dependencies=guard)
    def read(request: ReadRequest):
        return invoke("read", request)

    @app.post("/prepare", dependencies=guard)
    def prepare(request: PrepareRequest):
        return invoke("prepare", request)

    @app.post("/confirm", dependencies=guard)
    def confirm(request: ConfirmRequest):
        return invoke("confirm", request)

    @app.post("/cancel", dependencies=guard)
    def cancel(request: ConfirmRequest):
        return executor.cancel(request)

    @app.post("/operations/{task_id}", dependencies=guard)
    def operation(task_id: str, actor: Actor):
        return executor.operation_status(actor, task_id)

    return app
