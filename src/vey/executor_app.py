import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, Header
from sqlalchemy import text

from vey.api_common import install_errors
from vey.config import Settings
from vey.db import database
from vey.docker_backend import DockerBackend
from vey.domain import Actor, ConfirmRequest, PrepareRequest, ReadRequest
from vey.executor import Executor
from vey.security import verify_token


def create_app(settings=None, executor=None):
    settings = settings or Settings()
    engine = None
    if executor is None:
        engine, factory = database(settings.database_url.get_secret_value())
        executor = Executor(factory, settings.policy(), DockerBackend(settings), settings)

    @asynccontextmanager
    async def lifespan(app):
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

    app = FastAPI(
        title="Vey executor", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    install_errors(app)

    def authenticated(authorization: str | None = Header(default=None)):
        verify_token(authorization, settings.executor_token.get_secret_value())

    guard = [Depends(authenticated)]

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
        return executor.read(request)

    @app.post("/prepare", dependencies=guard)
    def prepare(request: PrepareRequest):
        return executor.prepare(request)

    @app.post("/confirm", dependencies=guard)
    def confirm(request: ConfirmRequest):
        return executor.confirm(request)

    @app.post("/cancel", dependencies=guard)
    def cancel(request: ConfirmRequest):
        return executor.cancel(request)

    @app.post("/operations/{task_id}", dependencies=guard)
    def operation(task_id: str, actor: Actor):
        return executor.operation_status(actor, task_id)

    return app
