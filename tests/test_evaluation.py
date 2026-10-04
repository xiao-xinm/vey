import asyncio
import copy
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from vey.domain import Intent, NextStep
from vey.evaluation.cli import main
from vey.evaluation.runner import compare_reports, run_evaluation
from vey.evaluation.schema import Dataset, Pricing, load_dataset
from vey.evaluation.scoring import aggregate, estimated_cost, percentile, score, usage_totals
from vey.model import DeepSeekProvider

DATASET = Path(__file__).parents[1] / "evals/datasets/phase2-v1.json"


def test_versioned_dataset_split_and_invalid_labels():
    dataset, _ = load_dataset(DATASET)
    assert len(dataset.cases) == 60
    assert sum(c.split == "dev" for c in dataset.cases) == 40
    assert sum(c.split == "test" for c in dataset.cases) == 20
    raw = dataset.model_dump()
    raw["cases"][-1]["family"] = raw["cases"][0]["family"]
    with pytest.raises(ValidationError, match="family"):
        Dataset.model_validate(raw)
    raw = dataset.model_dump()
    raw["cases"][1]["id"] = raw["cases"][0]["id"]
    with pytest.raises(ValidationError, match="Duplicate"):
        Dataset.model_validate(raw)
    raw = dataset.model_dump()
    raw["cases"][0]["expected"]["any_of"] = [{}]
    with pytest.raises(ValidationError, match="Empty output assertions"):
        Dataset.model_validate(raw)


def test_scoring_rejects_modification_and_invented_citations():
    dataset, _ = load_dataset(DATASET)
    negation = next(c for c in dataset.cases if c.id == "r17")
    failures = score(negation, {"kind": "operation", "target": "blog/web", "action": "restart"})
    assert "unsafe_operation_intent" in failures
    summary = next(c for c in dataset.cases if c.id == "p07")
    assert score(summary, {"tool": None, "summary": "E1 健康检查超时，无法确认业务健康。"}) == []
    assert "unknown_evidence_reference" in score(
        summary, {"tool": None, "summary": "E999 超时，无法判断"}
    )
    assert "missing_evidence_reference" in score(
        summary, {"tool": None, "summary": "超时，无法判断"}
    )


def test_missing_usage_or_cache_breakdown_never_becomes_free():
    pricing = Pricing(
        provider="fixture",
        model="test",
        currency="CNY",
        as_of="2026-10-04",
        source="test-only pricing, not vendor price",
        input_per_million=2,
        output_per_million=4,
        cached_input_per_million=0.2,
    )
    metric = {
        "model": "test",
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "total_tokens": 1100,
            "prompt_cache_hit_tokens": 500,
        },
    }
    assert estimated_cost([metric], pricing) == pytest.approx(0.0015)
    assert estimated_cost([{"model": "test", "usage": None}], pricing) is None
    assert estimated_cost([metric], None) is None
    no_cache = copy.deepcopy(metric)
    no_cache["usage"].pop("prompt_cache_hit_tokens")
    assert estimated_cost([no_cache], pricing) is None
    assert estimated_cost([], None) == 0
    assert usage_totals([metric, {"usage": None}])["total_tokens"] is None
    assert percentile([1, 2, 3, 4], 0.95) == 3.85


@pytest.mark.asyncio
async def test_rules_mode_reports_uncovered_cases_without_loading_provider(tmp_path):
    dataset, fingerprint = load_dataset(DATASET)

    def forbidden():
        raise AssertionError("Rules mode must not instantiate a provider")

    report = await run_evaluation(
        dataset,
        fingerprint,
        split="dev",
        strategy="hybrid",
        mode="rules",
        output=tmp_path / "rules",
        provider_factory=forbidden,
    )
    assert report["summary"]["failed"] == 0
    assert 0 < report["summary"]["executed"] < 40
    assert report["summary"]["not_run"] > 0
    assert report["attempted_model_calls"] == 0
    assert report["status"] == "incomplete"
    assert report["summary"]["actual_tool_executions"] == 0
    assert load_dataset(tmp_path / "rules/dataset.json")[1] == fingerprint
    assert (
        json.loads((tmp_path / "rules/report.json").read_text(encoding="utf-8"))["run_id"]
        == report["run_id"]
    )
    with pytest.raises(FileExistsError):
        await run_evaluation(
            dataset,
            fingerprint,
            split="dev",
            strategy="hybrid",
            mode="rules",
            output=tmp_path / "rules",
        )


@pytest.mark.asyncio
async def test_live_provider_receives_no_labels_and_budget_prevents_more_calls(tmp_path, settings):
    dataset, fingerprint = load_dataset(DATASET)
    seen = []

    def handler(request):
        body = json.loads(request.content)
        assert "expected" not in body["messages"][1]["content"]
        assert "rationale" not in body["messages"][1]["content"]
        seen.append(body)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"kind":"query","tool":{"name":"system"}}'},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            },
        )

    def provider():
        return DeepSeekProvider(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    report = await run_evaluation(
        dataset,
        fingerprint,
        split="dev",
        strategy="model",
        mode="live",
        output=tmp_path / "live",
        provider_factory=provider,
        max_model_calls=1,
        limit=2,
        model_name=settings.model_name,
    )
    assert len(seen) == 1
    assert report["summary"]["passed"] == 1
    assert report["results"][1]["skip_reason"] == "request_budget"
    assert report["summary"]["estimated_cost"] is None
    assert report["summary"]["coverage"] == 0.5
    assert settings.model_key.get_secret_value() not in (tmp_path / "live/report.json").read_text()


@pytest.mark.asyncio
async def test_invalid_model_output_is_failure_and_no_tool_is_executed(tmp_path, settings):
    dataset, fingerprint = load_dataset(DATASET)

    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"kind":"query","tool":{"name":"shell","command":"do bad things"}}'
                        },
                    }
                ]
            },
        )

    def provider():
        return DeepSeekProvider(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    report = await run_evaluation(
        dataset,
        fingerprint,
        split="dev",
        strategy="model",
        mode="live",
        output=tmp_path / "bad",
        provider_factory=provider,
        limit=1,
    )
    assert report["summary"]["failed"] == 1
    assert report["results"][0]["error_type"] == "model_failed"
    assert report["summary"]["tokens"]["total_tokens"] is None
    assert report["summary"]["actual_tool_executions"] == 0


@pytest.mark.asyncio
async def test_timeout_and_interruption_are_persisted(tmp_path):
    dataset, fingerprint = load_dataset(DATASET)

    class Slow:
        metrics = []

        async def route(self, *args):
            await asyncio.sleep(3)

        async def close(self):
            pass

    report = await run_evaluation(
        dataset,
        fingerprint,
        split="dev",
        strategy="model",
        mode="live",
        output=tmp_path / "slow",
        provider_factory=Slow,
        limit=1,
        timeout=1,
    )
    assert report["results"][0]["failures"] == ["sample_timeout"]
    assert report["summary"]["estimated_cost"] is None

    class Interrupted(Slow):
        async def route(self, *args):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await run_evaluation(
            dataset,
            fingerprint,
            split="dev",
            strategy="model",
            mode="live",
            output=tmp_path / "interrupt",
            provider_factory=Interrupted,
            limit=2,
        )
    interrupted = json.loads((tmp_path / "interrupt/report.json").read_text())
    assert interrupted["status"] == "interrupted"
    assert interrupted["summary"]["passed"] == 0


@pytest.mark.asyncio
async def test_planner_uses_fixtures_and_log_page_not_routed(tmp_path):
    dataset, fingerprint = load_dataset(DATASET)
    selected = [next(c for c in dataset.cases if c.id == i) for i in ("r39", "p19")]
    dataset = dataset.model_copy(update={"cases": selected})

    class Provider:
        metrics = []

        async def route(self, message, context, catalog):
            assert "log_page" not in context
            return Intent(kind="query", tool={"name": "logs", "target": "shop/api"})

        async def next_step(self, message, evidence, target, catalog):
            assert evidence[0]["id"] == "E1" and target == "shop/api"
            return NextStep(tool={"name": "logs", "target": "shop/api"})

        async def close(self):
            pass

    report = await run_evaluation(
        dataset,
        fingerprint,
        split="test",
        strategy="hybrid",
        mode="live",
        output=tmp_path / "fixtures",
        provider_factory=Provider,
    )
    assert report["summary"]["passed"] == 2
    assert report["summary"]["estimated_cost"] is None
    clone = copy.deepcopy(report)
    clone["strategy"] = "model"
    assert compare_reports(report, clone)["paired_executed"] == 2
    clone["split"] = "dev"
    with pytest.raises(ValueError, match="split"):
        compare_reports(report, clone)


def test_repeats_are_not_independent_cases():
    rows = [
        {
            "case_id": "one",
            "status": s,
            "duration_ms": 10,
            "failures": [] if s == "passed" else ["output_mismatch"],
            "model_metrics": [],
            "estimated_cost": 0,
            "actual": {},
        }
        for s in ["passed", "failed", "passed"]
    ]
    result = aggregate(rows)
    assert result["fully_executed_cases"] == 1
    assert result["all_repeats_passed_cases"] == 0
    assert result["varying_outcome_cases"] == 1
    assert result["contract_pass_rate"] == 2 / 3


def test_eval_cli_never_requires_production_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["validate", str(DATASET)]) == 0
    with pytest.raises(SystemExit):
        main(
            [
                "run",
                str(DATASET),
                "--split",
                "dev",
                "--mode",
                "live",
                "--output",
                str(tmp_path / "no-key"),
            ]
        )
