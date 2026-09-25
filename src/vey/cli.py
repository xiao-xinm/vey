import argparse
import json
import os
import time
import uuid
from pathlib import Path

import httpx
import uvicorn

from vey.config import Settings


def main():
    parser = argparse.ArgumentParser(prog="vey")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("component", choices=["core", "executor"])
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    sub.add_parser("check-config")
    message = sub.add_parser("message")
    message.add_argument("text")
    message.add_argument("--url", default="http://127.0.0.1:8080")
    message.add_argument("--wait", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    policy = settings.policy()
    if args.command == "check-config":
        print(
            json.dumps(
                {
                    "status": "valid",
                    "services": len(policy.services),
                    "model": settings.model_name,
                    "debug_enabled": settings.debug_enabled,
                },
                ensure_ascii=False,
            )
        )
    elif args.command == "serve":
        if args.component == "executor":
            os.umask(0o007)
            socket = Path(settings.executor_socket)
            if not socket.parent.is_dir():
                parser.error("执行器 socket 目录不存在，请按部署说明创建并配置权限")
            uvicorn.run(
                "vey.executor_app:create_app",
                factory=True,
                uds=str(socket),
                workers=1,
                access_log=False,
            )
        else:
            uvicorn.run(
                "vey.app:create_app",
                factory=True,
                host=args.host,
                port=args.port,
                workers=1,
                access_log=False,
            )
    elif args.command == "message":
        headers = {"Authorization": "Bearer " + settings.debug_token.get_secret_value()}
        with httpx.Client(base_url=args.url, headers=headers, timeout=10) as client:
            response = client.post(
                "/debug/messages",
                json={
                    "message_id": uuid.uuid4().hex,
                    "user_id": policy.owner_id,
                    "text": args.text,
                },
            )
            response.raise_for_status()
            result = response.json()
            print(json.dumps(result, ensure_ascii=False))
            if args.wait:
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    response = client.get("/debug/tasks/" + result["task_id"])
                    response.raise_for_status()
                    task = response.json()
                    if task["status"] not in {"queued", "control_queued", "running"}:
                        print(task["result"])
                        return
                    time.sleep(1)
                print("等待已超时，任务仍可按编号查询；不要重复提交修改请求。")


if __name__ == "__main__":
    main()
