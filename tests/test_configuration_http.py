import httpx

from vey.dashboard import DashboardSettings, create_app


async def test_configuration_bridge_requires_session_origin_and_bounded_body(tmp_path):
    calls = []

    class Management:
        def call(self, action="", payload=None):
            calls.append((action, payload))
            return {"ok": True}

        def close(self):
            pass

    secret = tmp_path / "password"
    secret.write_text("test-password-" + "x" * 40)
    settings = DashboardSettings(
        database_url="postgresql+psycopg://x@localhost/vey_test",
        password_file=secret,
        public_origin="https://ops.example.com",
    )
    app = create_app(settings, store=object(), configuration=Management())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.public_origin
    ) as client:
        assert (await client.get("/admin/api/configuration")).status_code == 401
        headers = {"Origin": settings.public_origin, "Content-Type": "application/json"}
        await client.post(
            "/admin/api/login", json={"password": secret.read_text()}, headers=headers
        )
        assert (await client.post("/admin/api/configuration/publish", json={})).status_code == 403
        assert (
            await client.post(
                "/admin/api/configuration/publish", content=b"x" * 65537, headers=headers
            )
        ).status_code == 413
        assert (
            await client.post(
                "/admin/api/configuration/preview", content=b"invalid", headers=headers
            )
        ).status_code == 400
        assert not calls
        assert (
            await client.post(
                "/admin/api/configuration/preview", json={"test": "payload"}, headers=headers
            )
        ).status_code == 200
        assert calls == [("/preview", {"test": "payload"})]
        assert (
            await client.post("/admin/api/configuration/confirm", json={}, headers=headers)
        ).status_code == 404
