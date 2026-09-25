import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import text

from vey.api_common import install_errors
from vey.config import Settings
from vey.core import Core
from vey.db import database
from vey.domain import DebugMessage, VeyError
from vey.executor_client import ExecutorClient
from vey.model import DeepSeekProvider
from vey.security import verify_token
from vey.wecom import WeComCipher, WeComSender, parse_xml


def create_app(settings=None, core=None, sender=None):
    settings = settings or Settings()
    engine = None
    if core is None:
        engine, factory = database(settings.database_url.get_secret_value())
        core = Core(
            factory, settings.policy(), ExecutorClient(settings), DeepSeekProvider(settings)
        )
    sender = sender or WeComSender(settings)
    cipher = (
        WeComCipher(
            settings.wecom_token.get_secret_value(),
            settings.wecom_aes_key.get_secret_value(),
            settings.wecom_corp_id,
        )
        if settings.wecom_aes_key.get_secret_value()
        else None
    )

    @asynccontextmanager
    async def lifespan(app):
        running = []
        if settings.worker_enabled:
            core.recover()

            async def deliveries():
                while True:
                    with suppress(Exception):
                        await core.send_pending(sender)
                    await asyncio.sleep(1)

            async def maintenance():
                while True:
                    with suppress(Exception):
                        core.cleanup()
                    await asyncio.sleep(30)

            running = [
                asyncio.create_task(core.loop()),
                asyncio.create_task(core.loop(control=True)),
                asyncio.create_task(deliveries()),
                asyncio.create_task(maintenance()),
            ]
        yield
        for task in running:
            task.cancel()
        for task in running:
            with suppress(asyncio.CancelledError):
                await task
        await sender.close()
        if engine:
            await core.executor.close()
            await core.model.close()
            engine.dispose()

    app = FastAPI(title="Vey", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    install_errors(app)

    @app.get("/health/live")
    def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready():
        with core.factory() as db:
            db.execute(text("SELECT id FROM vey_core.tasks LIMIT 0"))
            db.execute(text("SELECT id FROM vey_core.outbox LIMIT 0"))
        return {"status": "ready"}

    def debug_auth(authorization: str | None = Header(default=None)):
        if not settings.debug_enabled:
            raise VeyError("not_found", "接口不存在", 404)
        verify_token(authorization, settings.debug_token.get_secret_value())

    @app.post("/debug/messages", dependencies=[Depends(debug_auth)])
    def debug_message(message: DebugMessage):
        return core.accept(message.message_id, message.user_id, message.text)

    @app.get("/debug/tasks/{task_id}", dependencies=[Depends(debug_auth)])
    def debug_task(task_id: str):
        return core.task(task_id)

    def require_cipher():
        if cipher is None:
            raise VeyError("wecom_not_configured", "企微入口尚未配置", 503)
        return cipher

    @app.get("/wecom/callback", response_class=PlainTextResponse)
    def verify(msg_signature: str, timestamp: str, nonce: str, echostr: str):
        return require_cipher().decrypt(echostr, msg_signature, timestamp, nonce).decode()

    @app.post("/wecom/callback", response_class=PlainTextResponse)
    async def callback(request: Request, msg_signature: str, timestamp: str, nonce: str):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise VeyError("oversized_message", "消息过大", 413)
        envelope = parse_xml(bytes(body))
        encrypted = envelope.findtext("Encrypt")
        if not encrypted:
            raise VeyError("invalid_callback", "缺少加密内容")
        content = parse_xml(require_cipher().decrypt(encrypted, msg_signature, timestamp, nonce))
        if content.findtext("ToUserName") != settings.wecom_corp_id or content.findtext(
            "AgentID"
        ) != str(settings.wecom_agent_id):
            raise VeyError("wrong_application", "消息不属于此应用", 403)
        if content.findtext("MsgType") != "text":
            return ""
        message_id = content.findtext("MsgId")
        if not message_id:
            raise VeyError("missing_message_id", "消息缺少去重标识")
        core.accept(
            f"wecom:{message_id}",
            content.findtext("FromUserName", ""),
            content.findtext("Content", ""),
            "wecom",
        )
        # No model/tool waits in callback. The persisted outbox sends the result later.
        return ""

    return app
