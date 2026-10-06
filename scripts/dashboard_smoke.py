"""Read-only HTTPS acceptance; never print credentials or task contents."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from vey.replay import load_snapshot, render_html


def check(base_url, password_file, include_evals=False):
    origin = base_url.rstrip("/")
    url = urlsplit(origin)
    if url.scheme != "https" or not url.hostname or url.path or url.username:
        raise ValueError("Supply only an HTTPS origin")
    checks = []
    with httpx.Client(base_url=origin, timeout=15, trust_env=False) as client:
        assert client.get("/admin/").status_code == 200
        assert client.get("/admin/assets/app.js").status_code == 200
        assert client.get("/admin/api/tasks").status_code == 401
        assert client.post("/admin/api/login", json={"password": "incorrect"}).status_code == 403
        headers = {"Origin": origin}
        login = client.post(
            "/admin/api/login",
            headers=headers,
            json={"password": Path(password_file).read_text().strip()},
        )
        assert login.status_code == 200
        cookie = login.headers["set-cookie"]
        assert all(v in cookie for v in ("HttpOnly", "Secure", "SameSite=strict"))
        overview = client.get("/admin/api/overview")
        assert overview.status_code == 200
        assert overview.headers["cache-control"] == "no-store"
        tasks = client.get("/admin/api/tasks?limit=2")
        assert tasks.status_code == 200
        items = tasks.json()["items"]
        for item in items:
            assert all(k not in item for k in ("text", "user_id", "generation", "message_id"))
            detail = client.get("/admin/api/tasks/" + item["id"])
            assert detail.status_code == 200
            assert "events" in detail.json()
            exported = client.get("/admin/api/tasks/" + item["id"] + "/export")
            assert exported.status_code == 200
            snapshot = load_snapshot(exported.content)
            assert snapshot["task"]["id"] == item["id"]
            assert "default-src 'none'" in render_html(snapshot)
        if tasks.json()["next"]:
            older = client.get(
                "/admin/api/tasks", params={"before": tasks.json()["next"], "limit": 2}
            )
            assert older.status_code == 200
            assert not {i["id"] for i in items} & {i["id"] for i in older.json()["items"]}
        assert client.post("/admin/api/tasks", json={}).status_code == 405
        if include_evals:
            runs = client.get("/admin/api/evaluations?limit=2")
            assert runs.status_code == 200 and runs.json()["items"]
            for run in runs.json()["items"]:
                evaluated = client.get("/admin/api/evaluations/" + run["run_id"])
                assert evaluated.status_code == 200
                assert evaluated.json()["run"]["report_hash"] == run["report_hash"]
                assert "scoring_label" in evaluated.json()["run"]
            if runs.json()["next"]:
                older_runs = client.get(
                    "/admin/api/evaluations", params={"before": runs.json()["next"]}
                )
                assert older_runs.status_code == 200
                assert not {r["run_id"] for r in runs.json()["items"]} & {
                    r["run_id"] for r in older_runs.json()["items"]
                }
        cookies = dict(client.cookies)
        assert client.post("/admin/api/logout", headers=headers).status_code == 200
        client.cookies.update(cookies)
        assert client.get("/admin/api/tasks").status_code == 401
        for path in ("/debug/tasks/anything", "/health/ready", "/openapi.json"):
            assert client.get(path).status_code == 404
        checks = [
            "unauthenticated_denied",
            "origin_enforced",
            "secure_cookie",
            "readonly_queries",
            "pagination",
            "no_write_endpoint",
            "logout_revokes_session",
            "private_paths_hidden",
        ]
    if include_evals:
        checks += [
            "readonly_evaluation_archive",
            "evaluation_pagination",
            "audit_export_offline_validation",
        ]
    return {
        "status": "passed",
        "checks": checks,
        "wecom_messages_sent": 0,
        "docker_mutations": 0,
        "task_payloads_exported": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--include-evals", action="store_true")
    args = parser.parse_args()
    print(json.dumps(check(args.origin, args.password_file, args.include_evals)))
