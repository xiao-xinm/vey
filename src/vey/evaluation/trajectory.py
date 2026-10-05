"""Complete read-only trajectories against resettable, offline tool snapshots."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from vey.config import Policy
from vey.diagnosis import LOOP_VERSION, run_diagnosis
from vey.domain import NextStep, StrictModel, ToolCall, VeyError, digest, utcnow
from vey.evaluation.runner import implementation_hash
from vey.evaluation.scoring import usage_totals
from vey.logs import page_logs
from vey.security import safe_value


class ToolFixture(StrictModel):
    call: ToolCall
    result: dict | None = None
    error: str | None = None
    complete_log: bool = False

    @model_validator(mode="after")
    def exclusive(self):
        if (self.result is None) == (self.error is None):
            raise ValueError("Fixture requires exactly one result/error")
        if self.complete_log and (
            self.call.name != "logs"
            or self.call.keyword
            or self.call.since
            or self.call.until
            or not isinstance((self.result or {}).get("text"), str)
            or self.result.get("truncated")
            or self.result.get("cursor")
        ):
            raise ValueError("Complete log fixtures require unfiltered, untruncated text")
        return self


class TrajectoryCase(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    message: str = Field(min_length=1, max_length=4000)
    target: str
    fixtures: list[ToolFixture] = Field(min_length=1, max_length=12)
    script: list[NextStep] = Field(min_length=1, max_length=5)
    expected_stop: str
    expected_codes: list[str] = Field(default_factory=list)
    forbidden_supported_codes: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    provenance: str


class TrajectoryDataset(StrictModel):
    version: str
    scope: Literal["full_readonly_diagnosis"]
    policy: Policy
    cases: list[TrajectoryCase] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def check_cases(self):
        from vey.diagnosis import STOP_LABELS, permitted

        if len({c.id for c in self.cases}) != len(self.cases):
            raise ValueError("Duplicate case IDs")
        catalog = self.policy.catalog()
        for c in self.cases:
            if c.target not in {s["key"] for s in catalog} or c.expected_stop not in STOP_LABELS:
                raise ValueError("Unknown target/stop reason")
            keys = [digest(f.call.model_dump(mode="json")) for f in c.fixtures]
            if len(set(keys)) != len(keys) or not all(
                permitted(f.call, catalog) for f in c.fixtures
            ):
                raise ValueError("Duplicate or forbidden fixture call")
        return self


def load_trajectories(path: Path):
    if path.stat().st_size > 2_000_000:
        raise ValueError("Trajectory dataset exceeds 2 MB")
    return TrajectoryDataset.model_validate_json(path.read_text(encoding="utf-8"))


class ReplayTools:
    def __init__(self, fixtures):
        self.fixtures = {digest(f.call.model_dump(mode="json")): f for f in fixtures}

    async def read(self, call):
        fixture = self.fixtures.get(digest(call.model_dump(mode="json")))
        if (
            fixture is None
            and call.name == "logs"
            and not (call.since or call.until or call.cursor)
        ):
            candidates = [
                f for f in self.fixtures.values() if f.complete_log and f.call.target == call.target
            ]
            if len(candidates) == 1:
                source = candidates[0].result
                records = source["text"].splitlines()
                if call.keyword:
                    records = [r for r in records if call.keyword in r]
                page = page_logs(records, call.lines)
                return {
                    **source,
                    "text": page.text,
                    "truncated": page.truncated,
                    "cursor": None,
                    "replay_has_earlier": bool(page.remaining or page.pending),
                }
        if fixture is None:
            raise VeyError("fixture_unavailable", "此隔离场景没有该工具参数的快照")
        if fixture.error:
            raise VeyError(fixture.error, "隔离夹具模拟的工具失败：" + fixture.error)
        # Independent deep copy: one run cannot modify the next run's evidence.
        return json.loads(json.dumps(fixture.result))


class CountedPlanner:
    def __init__(self, provider):
        self.provider, self.calls = provider, 0

    async def next_step(self, *args):
        self.calls += 1
        return await self.provider.next_step(*args)


class ScriptedPlanner:
    def __init__(self, steps):
        self.steps = iter(steps)
        self.metrics = []

    async def next_step(self, *args):
        try:
            return next(self.steps).model_copy(deep=True)
        except StopIteration:
            raise VeyError("script_exhausted", "回放脚本已结束") from None

    async def close(self):
        pass


def score_trajectory(case, result):
    failures = []
    if result.stop_reason != case.expected_stop:
        failures.append("stop_reason_mismatch")
    codes = {h.code for h in result.diagnosis.hypotheses} if result.diagnosis else set()
    if not set(case.expected_codes).issubset(codes):
        failures.append("missing_hypothesis_code")
    if result.diagnosis and any(
        h.code in case.forbidden_supported_codes and h.confidence == "supported"
        for h in result.diagnosis.hypotheses
    ):
        failures.append("unsupported_causal_claim")
    tools = {e["tool"]["name"] for e in result.evidence if "result" in e}
    if not set(case.required_tools).issubset(tools):
        failures.append("missing_successful_tool")
    if any(e.get("error") == "fixture_unavailable" for e in result.evidence):
        failures.append("unavailable_fixture_requested")
    return failures


def write_trajectory_report(output, report):
    temp = output / "report.json.tmp"
    temp.write_text(json.dumps(safe_value(report), ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(output / "report.json")


async def run_trajectories(
    dataset, output, *, provider_factory=None, max_model_calls=100, timeout=60
):
    if not 0 <= max_model_calls <= 500 or not 0 < timeout <= 60:
        raise ValueError("Invalid trajectory bounds")
    output.mkdir(parents=True, exist_ok=False)
    (output / "dataset.json").write_text(dataset.model_dump_json(indent=2), encoding="utf-8")
    report = {
        "run_id": uuid.uuid4().hex,
        "scope": dataset.scope,
        "dataset_version": dataset.version,
        "dataset_hash": digest(dataset.model_dump(mode="json")),
        "implementation_hash": implementation_hash(),
        "loop_version": LOOP_VERSION,
        "mode": "live" if provider_factory else "scripted",
        "started_at": utcnow().isoformat(),
        "status": "running",
        "actual_docker_calls": 0,
        "model_calls": 0,
        "scheduled": len(dataset.cases),
        "max_model_calls": max_model_calls,
        "trajectory_timeout_seconds": timeout,
        "results": [],
    }
    write_trajectory_report(output, report)
    try:
        for case in dataset.cases:
            # Reserve the worst-case budget before starting a trajectory.
            if provider_factory and report["model_calls"] + 5 > max_model_calls:
                report["status"] = "budget_exhausted"
                break
            provider = provider_factory() if provider_factory else ScriptedPlanner(case.script)
            started = time.monotonic()
            counted = CountedPlanner(provider)
            evidence = []
            result = None
            try:
                result = await run_diagnosis(
                    counted,
                    case.message,
                    case.target,
                    dataset.policy.catalog(),
                    ReplayTools(case.fixtures).read,
                    evidence=evidence,
                    timeout=timeout,
                )
            finally:
                requests = counted.calls if provider_factory else 0
                report["model_calls"] += requests
                metrics = list(getattr(provider, "metrics", []))
                while len(metrics) < requests:
                    metrics.append({"status": "unknown", "usage": None})
                failures = (
                    score_trajectory(case, result)
                    if result
                    else ["trajectory_interrupted_or_error"]
                )
                report["results"].append(
                    {
                        "case_id": case.id,
                        "status": "failed" if failures else "passed",
                        "failures": failures,
                        "duration_ms": round((time.monotonic() - started) * 1000, 2),
                        "metrics": safe_value(metrics),
                        "prompt_version": getattr(provider, "prompt_version", None),
                        **(
                            result.as_dict()
                            if result
                            else {"evidence": evidence, "stop_reason": "interrupted_or_error"}
                        ),
                        "model_calls": requests,
                        "planner_steps": counted.calls,
                    }
                )
                write_trajectory_report(output, report)
                await provider.close()
        else:
            report["status"] = "completed"
    except (asyncio.CancelledError, KeyboardInterrupt):
        report["status"] = "interrupted"
        raise
    except Exception:
        report["status"] = "error"
        raise
    finally:
        report["finished_at"] = utcnow().isoformat()
        report["executed"] = len(report["results"])
        report["not_run"] = report["scheduled"] - report["executed"]
        report["tokens"] = usage_totals([m for row in report["results"] for m in row["metrics"]])
        report["estimated_cost"] = None if report["model_calls"] else 0
        report["passed"] = sum(r["status"] == "passed" for r in report["results"])
        write_trajectory_report(output, report)
    return report
