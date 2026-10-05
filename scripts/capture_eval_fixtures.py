"""Create/reset a unique isolated Compose project and capture only its evidence.

Run on a Docker host with an existing Python image. No model or DB credentials.
"""

import argparse
import json
import os
import subprocess
import time
import uuid
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists")
    root = Path(__file__).resolve().parents[1]
    project = "vey-eval-" + uuid.uuid4().hex[:12]
    env = {**os.environ, "VEY_FIXTURE_IMAGE": args.image}
    compose = ["docker", "compose", "-f", str(root / "evals/fixtures/compose.yaml"), "-p", project]

    def run(command):
        return subprocess.check_output(command, env=env, text=True, timeout=60).strip()

    run(["docker", "image", "inspect", args.image])
    dataset = json.loads((root / "evals/datasets/trajectories-v1.json").read_text(encoding="utf-8"))
    dataset["cases"] = [c for c in dataset["cases"] if c["id"] in {"exit", "dependency", "config"}]
    dataset["version"] = "docker-trajectories-v1"
    captures = []
    try:
        run([*compose, "up", "-d", "--no-build"])
        for case in dataset["cases"]:
            container_id = run([*compose, "ps", "-a", "-q", case["id"]])
            for _ in range(20):
                item = json.loads(run(["docker", "inspect", container_id]))[0]
                if item["Config"]["Labels"].get("com.docker.compose.project") != project:
                    raise RuntimeError("Unexpected project ownership")
                if item["State"]["Status"] == "exited":
                    break
                time.sleep(0.25)
            else:
                raise RuntimeError("Fixture failed to reach exited state")
            expected_exit = {"exit": 7, "dependency": 3, "config": 2}[case["id"]]
            if item["State"]["ExitCode"] != expected_exit:
                raise RuntimeError("Unexpected fixture exit code")
            logs = run(["docker", "logs", "--tail", "100", container_id])[:10000]
            case["fixtures"][0]["result"]["instances"][0].update(
                status=item["State"]["Status"],
                exit_code=item["State"]["ExitCode"],
                oom_killed=item["State"]["OOMKilled"],
            )
            case["fixtures"][1]["result"]["text"] = logs
            case["provenance"] = (
                "captured isolated Docker fixture; identifiers normalized to synthetic catalog"
            )
            captures.append(
                {
                    "scenario": case["id"],
                    "image_id": item["Image"],
                    "exit_code": item["State"]["ExitCode"],
                    "started_at": item["State"]["StartedAt"],
                }
            )
    finally:
        run([*compose, "down", "--timeout", "3"])
    args.output.mkdir(parents=True)
    (args.output / "dataset.json").write_text(
        json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.output / "capture.json").write_text(
        json.dumps(
            {"project": project, "captures": captures, "cleanup": "compose down succeeded"},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps({"scenarios": len(captures), "output": str(args.output), "cleanup": "completed"})
    )


if __name__ == "__main__":
    main()
