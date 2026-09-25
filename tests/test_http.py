import base64
import time

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

# Reuse the complete core/executor fixtures, backed by real PostgreSQL.
from vey.app import create_app
from vey.db import Outbox, Task
from vey.executor_app import create_app as executor_app
from vey.wecom import WeComCipher

pytestmark = pytest.mark.postgres


async def test_wecom_callback_auth_application_and_dedup(core, settings):
    settings.wecom_corp_id = "corp"
    settings.wecom_agent_id = 12
    settings.wecom_token = SecretStr("wecom-test-token")
    settings.wecom_aes_key = SecretStr(base64.b64encode(b"a" * 32).decode().rstrip("="))
    cipher = WeComCipher("wecom-test-token", settings.wecom_aes_key.get_secret_value(), "corp")
    app = create_app(settings, core)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:

        async def post(user="owner", agent=12, tamper=False):
            xml = f"<xml><ToUserName>corp</ToUserName><FromUserName>{user}</FromUserName><AgentID>{agent}</AgentID><MsgType>text</MsgType><MsgId>100</MsgId><Content>重启 博客</Content></xml>".encode()
            encrypted = cipher.encrypt(xml)
            timestamp, nonce = str(int(time.time())), "nonce"
            signature = cipher.signature(timestamp, nonce, encrypted)
            return await client.post(
                "/wecom/callback",
                params={
                    "msg_signature": "invalid" if tamper else signature,
                    "timestamp": timestamp,
                    "nonce": nonce,
                },
                content=f"<xml><Encrypt>{encrypted}</Encrypt></xml>",
            )

        assert (await post(tamper=True)).status_code == 403
        assert (await post(agent=99)).status_code == 403
        assert (await post(user="intruder")).status_code == 403
        for _ in range(2):
            response = await post()
            assert response.status_code == 200 and response.text == ""
        with core.factory() as db:
            assert db.scalar(select(func.count()).select_from(Task)) == 1
            assert db.scalar(select(func.count()).select_from(Outbox)) == 1
        assert (await client.post("/debug/messages", json={})).status_code == 401
        settings.debug_enabled = False
        assert (
            await client.get("/debug/tasks/any", headers={"Authorization": "Bearer " + "y" * 40})
        ).status_code == 404


async def test_executor_has_no_shell_or_unprotected_entrypoint(executor, actor, settings, backend):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=executor_app(settings, executor)),
        base_url="http://executor",
    ) as client:
        assert (await client.get("/health")).status_code == 401
        headers = {"Authorization": "Bearer " + "x" * 40}
        assert (
            await client.post(
                "/read",
                headers=headers,
                json={
                    "actor": actor.model_dump(mode="json"),
                    "call": {"name": "shell", "command": "docker restart postgres"},
                },
            )
        ).status_code == 422
        assert (await client.post("/exec", headers=headers)).status_code == 404
        assert (await client.get("/health", headers=headers)).status_code == 200
        assert not backend.actions
