"""Run with sudo on the deployed Linux host. Publish a temporary protected-service alias,
query it through the real core worker, then restore the original configuration as a new
version. No Docker mutation, model request or WeCom delivery is requested.
"""

import copy
import http.cookiejar
import json
import pathlib
import subprocess
import urllib.error
import urllib.request
import uuid


def main():
    root = pathlib.Path("/opt/vey")
    host = next(
        x.split("=", 1)[1].strip().strip('"')
        for x in (root / ".env").read_text().splitlines()
        if x.startswith("VEY_PUBLIC_HOST=")
    )
    if "/" in host or "@" in host or ":" in host:
        raise ValueError("Expected configured public hostname")
    origin = "https://" + host
    client = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )

    def api(path, data=None):
        request = urllib.request.Request(
            origin + "/admin/api/" + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Origin": origin, "Content-Type": "application/json"},
        )
        with client.open(request, timeout=35) as response:
            return json.load(response)

    def rejection(data):
        try:
            api("configuration/preview", data)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)["error"]
        raise AssertionError("Invalid configuration accepted")

    def publish(change):
        preview = api("configuration/preview", change)
        return api("configuration/publish", {**change, "proof": preview["proof"]})

    api("login", {"password": (root / "secrets/dashboard_password").read_text().strip()})
    before = api("configuration")
    run_id = uuid.uuid4().hex
    changed = copy.deepcopy(before["services"])
    service = next(s for s in changed if s["key"] == "vey/agent-core")
    assert service["protected"]
    alias = "配置验收" + run_id[:6]
    service["aliases"].append(alias)
    change = {
        "base_revision": before["revision"],
        "services": changed,
        "reason": "配置发布验收 " + run_id,
        "rollback_of": None,
    }
    invalid = copy.deepcopy(change)
    next(s for s in invalid["services"] if s["key"] == "vey/agent-core")["protected"] = False
    assert rejection(invalid) == (403, "protected_policy")
    invalid = copy.deepcopy(change)
    invalid["services"].append(
        {"key": "vey-not-deployed/web", "aliases": [], "protected": True, "health_url": None}
    )
    assert rejection(invalid) == (400, "not_deployed")
    published = None
    report = {
        "run_id": run_id,
        "protected_rejected": True,
        "missing_service_rejected": True,
        "baseline_revision": before["revision"],
    }
    try:
        published = publish(change)
        report["published_revision"] = published["revision"]
        source = """
import json,time,uuid
from sqlalchemy import select
from vey.config import Settings
from vey.core import Core
from vey.db import database,Event
s=Settings();engine,factory=database(s.database_url.get_secret_value());core=Core(factory,s.policy(),None,None)
r=core.accept('configuration-smoke:'+uuid.uuid4().hex,s.policy().owner_id,MESSAGE)
deadline=time.monotonic()+35
while time.monotonic()<deadline:
    task=core.task(r['task_id'])
    if task['status'] not in ('queued','control_queued','running'):break
    time.sleep(.2)
assert task['status']=='succeeded',task['status']
with factory() as db:
    events=list(db.scalars(select(Event).where(Event.task_id==r['task_id'])))
    tools=[e.data['call']['name'] for e in events if e.kind=='tool']
    models=sum(e.kind=='model' for e in events)
assert tools==['inspect'] and models==0
print(json.dumps({'task_id':r['task_id'],'status':task['status'],'tools':tools,'model_calls':models}))
engine.dispose()
""".replace("MESSAGE", repr("状态 " + alias))
        result = subprocess.run(
            ["docker", "exec", "-i", "vey-agent-core-1", "python", "-"],
            input=source,
            text=True,
            capture_output=True,
            timeout=50,
        )
        if result.returncode:
            raise RuntimeError("Core dynamic-policy query failed")
        report["core_query"] = json.loads(result.stdout)
    finally:
        current = api("configuration")
        # Resolve a lost publication response only when the uniquely tagged version and
        # parent match this test; never overwrite another administrator's later changes.
        our_version = next(
            (
                v
                for v in current["history"]
                if v["id"] == current["revision"]
                and v["reason"] == change["reason"]
                and v["parent_id"] == before["revision"]
            ),
            None,
        )
        if our_version:
            rollback = publish(
                {
                    "base_revision": current["revision"],
                    "services": before["services"],
                    "reason": "验收回滚 " + run_id,
                    "rollback_of": before["revision"],
                }
            )
            report["rollback_revision"] = rollback["revision"]
        final = api("configuration")
        assert final["services"] == before["services"], (
            "Concurrent change detected; manual review required"
        )
        api("logout", {})
    assert report.get("rollback_revision") and report["rollback_revision"] != before["revision"]
    report["status"] = "passed"
    directory = root / ".local/configuration-smoke"
    directory.mkdir(mode=0o700, exist_ok=True)
    output = directory / (run_id + ".json")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    output.chmod(0o600)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
