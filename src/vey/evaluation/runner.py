from __future__ import annotations

import asyncio
import importlib.metadata
import json
import platform
import time
import uuid
from collections import defaultdict
from pathlib import Path

from vey.domain import ToolCall, VeyError, digest, utcnow
from vey.evaluation.schema import Dataset, Pricing
from vey.evaluation.scoring import aggregate, estimated_cost, score
from vey.jev import JevRouter
from vey.model import (
    OPERATION_GUARD_VERSION,
    DeepSeekProvider,
    deterministic_intent,
    guard_operation_intent,
)
from vey.security import safe_value


def implementation_hash():
    root = Path(__file__).parents[1]
    return digest(
        {
            p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(root.rglob("*.py"))
        }
    )


def build_report(metadata: dict, results: list[dict]):
    groups = defaultdict(list)
    for result in results:
        groups[f"{result['stage']}/{result['category']}"].append(result)
    return {
        **metadata,
        "summary": aggregate(results),
        "by_category": {key: aggregate(rows) for key, rows in sorted(groups.items())},
        "results": results,
    }


def save_report(directory: Path, report: dict):
    # Incremental, atomic replacement leaves completed samples readable after interruption.
    temporary = directory / "report.json.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(directory / "report.json")
    summary = report["summary"]
    lines = [
        "# Vey 路由与下一步规划契约评测",
        "",
        f"运行：`{report['run_id']}`；数据：`{report['dataset_version']}`；分组：{report['split']}。",
        f"方案：{report['strategy']}；模式：{report['mode']}；状态：{report['status']}。",
        "",
        f"执行 {summary['executed']}/{summary['scheduled']}，通过 {summary['passed']}，失败 {summary['failed']}，未运行 {summary['not_run']}。",
        f"P50/P95：{summary['latency_ms_p50']}/{summary['latency_ms_p95']} ms；模型请求 {summary['model_calls']} 次。",
        f"用量：{json.dumps(summary['tokens'])}；估算费用：{summary['estimated_cost']}（null 表示未知）。",
        "",
        "这是路由／单步规划的确定性契约通过率，不是端到端诊断准确率。没有执行 Docker 工具或发送企微消息。",
        "规则模式中需要模型的样本明确记为未运行；未运行样本不会算作通过。时间包含客户端请求开销。",
        "区间按独立案例的全部重复是否通过计算，不能把同一案例的重复当作独立题目；小规模人工案例不代表线上分布。",
        "文本关键词断言只验证指定事实／不确定性提示，不证明自然语言结论正确；仍需人工复核失败与通过的诊断样本。",
        "",
        "| 类别 | 执行/计划 | 通过 | 失败 | P50 ms | P95 ms |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, group in report["by_category"].items():
        lines.append(
            f"| {name} | {group['executed']}/{group['scheduled']} | {group['passed']} | {group['failed']} | {group['latency_ms_p50']} | {group['latency_ms_p95']} |"
        )
    lines.extend(["", "## 失败与未运行", ""])
    for row in report["results"]:
        if row["status"] != "passed":
            lines.append(
                f"- `{row['case_id']}` 第 {row['repeat']} 次：{row['status']}；{', '.join(row['failures']) or row.get('skip_reason', '')}。"
            )
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run_evaluation(
    dataset: Dataset,
    dataset_hash: str,
    *,
    split: str,
    strategy: str,
    mode: str,
    output: Path,
    provider_factory=None,
    model_name: str | None = None,
    repeats: int = 1,
    max_model_calls: int = 100,
    timeout: float = 25,
    limit: int | None = None,
    pricing: Pricing | None = None,
    jev_pricing: Pricing | None = None,
    routing_config: dict | None = None,
    progress=None,
):
    if strategy not in {"hybrid", "model", "jev"} or mode not in {"rules", "live"}:
        raise ValueError("Unknown evaluation strategy or mode")
    if split not in {"dev", "test"} or not 1 <= repeats <= 5 or not 1 <= timeout <= 60:
        raise ValueError("Invalid evaluation bounds")
    if not 0 <= max_model_calls <= 500 or (limit is not None and limit < 1):
        raise ValueError("Invalid request budget or limit")
    if mode == "live" and provider_factory is None:
        raise ValueError("Live evaluation requires an explicit provider")
    selected = [case for case in dataset.cases if case.split == split]
    if limit is not None:
        selected = selected[:limit]
    if not selected:
        raise ValueError("No selected cases")
    output.mkdir(parents=True, exist_ok=False)
    (output / "dataset.json").write_text(dataset.model_dump_json(indent=2), encoding="utf-8")
    metadata = {
        "schema_version": 1,
        "run_id": uuid.uuid4().hex,
        "started_at": utcnow().isoformat(),
        "status": "running",
        "dataset_version": dataset.version,
        "dataset_hash": dataset_hash,
        "implementation_hash": implementation_hash(),
        "prompt_version": DeepSeekProvider.prompt_version,
        "operation_guard_version": OPERATION_GUARD_VERSION,
        "tool_schema_hash": digest(ToolCall.model_json_schema()),
        "scope": dataset.scope,
        "split": split,
        "strategy": strategy,
        "mode": mode,
        "model": model_name if mode == "live" else None,
        "repeats": repeats,
        "max_model_calls": max_model_calls,
        "sample_timeout_seconds": timeout,
        "selection_limit": limit,
        "pricing": pricing.model_dump(mode="json") if pricing else None,
        "jev_pricing": jev_pricing.model_dump(mode="json") if jev_pricing else None,
        "routing_config": routing_config,
        "router_prompt_version": JevRouter.prompt_version if strategy == "jev" else None,
        "versions": {
            "python": platform.python_version(),
            **{p: importlib.metadata.version(p) for p in ("pydantic", "httpx")},
        },
    }
    rows = [
        {
            "case_id": case.id,
            "stage": case.stage,
            "category": case.category,
            "repeat": repeat,
            "status": "not_run",
            "skip_reason": "pending",
            "failures": [],
            "duration_ms": 0,
            "actual": {},
            "model_metrics": [],
            "estimated_cost": None,
        }
        for repeat in range(1, repeats + 1)
        for case in selected
    ]
    cases = {case.id: case for case in selected}
    calls = 0
    save_report(output, build_report(metadata, rows))
    try:
        for row in rows:
            case, started, provider = cases[row["case_id"]], time.monotonic(), None
            row.pop("skip_reason", None)
            try:
                actual = None
                if strategy in {"hybrid", "jev"} and case.stage == "route":
                    actual = deterministic_intent(case.message, dataset.policy, case.context)
                    row["route_source"] = "rule" if actual else "model"
                if actual is None:
                    # Reserve both Jev and possible DeepSeek extraction before starting.
                    required = 2 if strategy == "jev" and case.stage == "route" else 1
                    if mode == "rules" or calls + required > max_model_calls:
                        row["skip_reason"] = (
                            "requires_model" if mode == "rules" else "request_budget"
                        )
                        continue
                    provider = provider_factory()
                    calls += 1
                    row["route_source"] = (
                        "jev" if strategy == "jev" and case.stage == "route" else "model"
                    )
                    async with asyncio.timeout(timeout):
                        if case.stage == "route":
                            # Match production: log page cache never goes to intent classification.
                            actual = await provider.route(
                                case.message,
                                {k: v for k, v in case.context.items() if k != "log_page"},
                                dataset.policy.catalog(),
                            )
                        else:
                            actual = await provider.next_step(
                                case.message, case.evidence, case.target, dataset.policy.catalog()
                            )
                if case.stage == "route":
                    row["proposed_intent"] = safe_value(actual.model_dump(mode="json"))
                    guarded = guard_operation_intent(case.message, actual, dataset.policy)
                    row["guard_changed_intent"] = guarded is not actual
                    actual = guarded
                row["actual"] = safe_value(actual.model_dump(mode="json"))
                row["failures"] = score(case, row["actual"])
                targets = {service.key for service in dataset.policy.services}
                tool = row["actual"].get("tool") or {}
                for target in (row["actual"].get("target"), tool.get("target")):
                    if target and target not in targets:
                        row["failures"].append("target_outside_catalog")
                row["status"] = "failed" if row["failures"] else "passed"
            except VeyError as exc:
                row["actual"] = {"error": exc.code}
                row["failures"] = score(case, row["actual"])
                row["status"] = "failed" if row["failures"] else "passed"
                row["error_type"] = exc.code
            except TimeoutError:
                row.update(status="failed", failures=["sample_timeout"], error_type="TimeoutError")
            except Exception as exc:
                # Never persist exception messages, URLs, headers or credentials.
                row.update(
                    status="failed", failures=["evaluation_error"], error_type=type(exc).__name__
                )
            finally:
                row["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
                if provider is not None:
                    row["model_metrics"] = safe_value(list(provider.metrics))
                    if not row["model_metrics"]:
                        row["model_metrics"] = [
                            {"model": model_name, "status": "unknown", "usage": None}
                        ]
                    calls += len(row["model_metrics"]) - 1
                    row["routing_metrics"] = safe_value(
                        list(getattr(provider, "routing_metrics", []))
                    )
                    try:
                        await provider.close()
                    except Exception:
                        row["close_failed"] = True
                if row["status"] != "not_run":
                    row["estimated_cost"] = mixed_cost(row["model_metrics"], pricing, jev_pricing)
                save_report(output, build_report(metadata, rows))
                if progress:
                    progress({k: row[k] for k in ("case_id", "repeat", "status", "failures")})
        metadata["status"] = (
            "completed" if all(r["status"] != "not_run" for r in rows) else "incomplete"
        )
    finally:
        if metadata["status"] == "running":
            metadata["status"] = "interrupted"
        metadata["finished_at"] = utcnow().isoformat()
        metadata["attempted_model_calls"] = calls
        report = build_report(metadata, rows)
        save_report(output, report)
    return report


def mixed_cost(metrics, pricing, jev_pricing):
    if not metrics:
        return 0.0
    costs, currencies = [], set()
    for metric in metrics:
        basis = jev_pricing if metric.get("provider") == "typesafe" else pricing
        cost = estimated_cost([metric], basis)
        if cost is None:
            return None
        costs.append(cost)
        currencies.add(basis.currency)
    return round(sum(costs), 8) if len(currencies) == 1 else None


def compare_reports(left: dict, right: dict):
    for key in ("dataset_hash", "scope", "split"):
        if left[key] != right[key]:
            raise ValueError(f"Cannot compare different {key}")
    if left["mode"] != "live" or right["mode"] != "live":
        raise ValueError("Model comparisons require live runs; rules-only is a coverage check")

    def index(report):
        return {(r["case_id"], r["repeat"]): r for r in report["results"]}

    a, b = index(left), index(right)
    if a.keys() != b.keys():
        raise ValueError("Paired comparisons require identical cases and repetitions")
    pairs = [
        (a[k], b[k])
        for k in sorted(a)
        if a[k]["status"] != "not_run" and b[k]["status"] != "not_run"
    ]
    return {
        "left_run": left["run_id"],
        "right_run": right["run_id"],
        "dataset_hash": left["dataset_hash"],
        "split": left["split"],
        "paired_executed": len(pairs),
        "unpaired_or_not_run": len(a) - len(pairs),
        "left_pass_right_fail": [
            x["case_id"] for x, y in pairs if x["status"] == "passed" and y["status"] != "passed"
        ],
        "left_fail_right_pass": [
            x["case_id"] for x, y in pairs if x["status"] != "passed" and y["status"] == "passed"
        ],
        "left": {k: left[k] for k in ("strategy", "model", "implementation_hash", "summary")},
        "right": {k: right[k] for k in ("strategy", "model", "implementation_hash", "summary")},
        "limits": "Single-step fixture contracts, not full diagnosis accuracy; sequential API runs are not a controlled latency experiment.",
    }
