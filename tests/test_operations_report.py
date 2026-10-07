import json
from datetime import UTC, datetime, timedelta

import httpx

from vey.dashboard import DashboardSettings, create_app
from vey.operations_report import operations_status


def test_reports_missing_invalid_stale_and_secret_filtering(tmp_path):
    assert operations_status(None) == {"configured": False}
    assert operations_status(tmp_path)["latest"]["state"] == "missing"
    latest = tmp_path / "latest.json"
    latest.write_bytes(b"x" * 65537)
    assert operations_status(tmp_path)["latest"]["state"] == "invalid"
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    report = {
        "status": "failed",
        "run_id": "a" * 32,
        "started_at": old,
        "stage": "restore_vey",
        "databases": {},
        "offsite": False,
        "encrypted": False,
        "scheduled": False,
        "password": "secret-must-disappear",
    }
    latest.write_text(json.dumps(report))
    parsed = operations_status(tmp_path)
    assert parsed["latest"]["stale"]
    assert "secret-must-disappear" not in json.dumps(parsed)
    (tmp_path / "last-success.json").write_text(json.dumps(report))
    assert operations_status(tmp_path)["last_success"]["state"] == "invalid"
    report["status"] = "passed"
    latest.write_text(json.dumps(report))
    assert operations_status(tmp_path)["latest"]["state"] == "invalid"
    report["status"] = "running"
    report["started_at"] = datetime.now().isoformat()
    latest.write_text(json.dumps(report))
    assert operations_status(tmp_path)["latest"]["state"] == "invalid"


async def test_operations_endpoint_requires_login_and_is_read_only(tmp_path):
    password = "independent-test-password-" + "a" * 40
    secret = tmp_path / "password"
    secret.write_text(password)
    settings = DashboardSettings(
        database_url="postgresql+psycopg://x@localhost/vey_test",
        password_file=secret,
        public_origin="https://ops.example.com",
        operations_report_dir=tmp_path,
    )
    app = create_app(settings, store=object())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.public_origin
    ) as client:
        assert (await client.get("/admin/api/operations")).status_code == 401
        assert (
            await client.post(
                "/admin/api/login",
                json={"password": password},
                headers={"origin": settings.public_origin},
            )
        ).status_code == 200
        response = await client.get("/admin/api/operations")
        assert response.status_code == 200
        assert response.json()["latest"]["state"] == "missing"
        assert response.headers["cache-control"] == "no-store"
        assert (await client.post("/admin/api/operations")).status_code == 405
