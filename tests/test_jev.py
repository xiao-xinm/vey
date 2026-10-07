import asyncio
import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from vey.db import Event
from vey.domain import Action, Intent, ToolCall, VeyError
from vey.evaluation.runner import mixed_cost, run_evaluation
from vey.evaluation.schema import Dataset, Pricing, load_dataset
from vey.jev import CRITERIA, JevEvaluationProvider, JevRouter, build_router


def reply(choice="system", probability=1.0):
    return {
        "model": "jev-test",
        "answers": {
            "route": {
                "type": "choice",
                "choice": choice,
                "confidence": max(0, (probability - 1 / 6) / (1 - 1 / 6)),
                "probabilities": {
                    k: probability if k == choice else (1 - probability) / 5 for k in CRITERIA
                },
            }
        },
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


class Fallback:
    def __init__(self, intent=None):
        self.calls, self.metrics = [], []
        self.intent = intent or Intent(kind="query", tool=ToolCall(name="services"))
        self.closed = False

    async def route(self, *args):
        self.calls.append(args)
        self.metrics.append(
            {"provider": "deepseek", "model": "test", "status": "ok", "usage": None}
        )
        return self.intent

    async def close(self):
        self.closed = True


def make_router(settings, handler, fallback=None):
    settings.jev_key = SecretStr("jev-test-key")
    return JevRouter(
        settings, fallback or Fallback(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


@pytest.mark.asyncio
async def test_direct_choice_redacts_and_skips_deepseek(settings, policy):
    seen = []

    def handler(request):
        seen.append(request)
        assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
        assert request.headers["Authorization"] == "Bearer jev-test-key"
        body = json.loads(request.content)
        assert body["state"]["catalog"] == policy.catalog()
        assert "log_page" not in body["state"]["context"]
        assert "private-value" not in request.content.decode()
        return httpx.Response(200, json=reply())

    router = make_router(settings, handler)
    result = await router.route(
        "服务器内存如何 token=private-value",
        {"log_page": {"text": "private-value"}},
        policy.catalog(),
    )
    assert result.tool.name == "system"
    assert not router.fallback.calls and len(seen) == 1
    assert router.metrics[0]["route_path"] == "jev_direct"
    assert router.metrics[0]["usage"]["total_tokens"] == 120
    assert "jev-test-key" not in json.dumps(router.metrics)
    await router.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "401",
        "429",
        "529",
        "redirect",
        "timeout",
        "deadline",
        "json",
        "shape",
        "choice",
        "probability",
        "confidence",
        "oversize",
    ],
)
async def test_failed_choice_falls_back_once_without_retry(settings, fault):
    seen = []

    async def handler(request):
        seen.append(request)
        if fault.isdigit():
            return httpx.Response(int(fault), text="secret upstream message")
        if fault == "redirect":
            return httpx.Response(302, headers={"location": "https://example.com/stolen"})
        if fault == "timeout":
            raise httpx.ReadTimeout("secret error")
        if fault == "deadline":
            await asyncio.sleep(1)
        if fault == "json":
            return httpx.Response(200, text="secret invalid body")
        if fault == "oversize":
            return httpx.Response(200, content=b"x" * 65537)
        body = reply()
        if fault == "shape":
            body["answers"] = {}
        if fault == "choice":
            body["answers"]["route"]["choice"] = "shell"
        if fault == "probability":
            body["answers"]["route"]["probabilities"]["system"] = float("nan")
            return httpx.Response(200, content=json.dumps(body).encode())
        if fault == "confidence":
            body["answers"]["route"]["confidence"] = 0.1
        return httpx.Response(200, json=body)

    settings.jev_timeout = 0.02
    router = make_router(settings, handler)
    assert (await router.route("test", {}, [])).tool.name == "services"
    assert len(router.fallback.calls) == len(seen) == 1
    assert router.metrics[0]["status"] == "error"
    assert "secret" not in json.dumps(router.metrics)
    await router.close()


@pytest.mark.asyncio
async def test_low_confidence_and_missing_key_fallback(settings):
    router = make_router(settings, lambda _: httpx.Response(200, json=reply(probability=0.5)))
    await router.route("test", {}, [])
    assert router.metrics[-1]["fallback_reason"] == "low_confidence"
    settings.jev_key = SecretStr("")
    await router.route("test", {}, [])
    assert router.metrics[-1]["request_sent"] is False
    assert router.metrics[-1]["fallback_reason"] == "not_configured"
    assert len(router.fallback.calls) == 2
    await router.close()


@pytest.mark.asyncio
async def test_cancellation_propagates_without_fallback(settings):
    async def handler(_):
        raise asyncio.CancelledError()

    router = make_router(settings, handler)
    with pytest.raises(asyncio.CancelledError):
        await router.route("test", {}, [])
    assert not router.fallback.calls
    assert router.metrics[-1]["status"] == "cancelled"
    await router.close()


@pytest.mark.asyncio
async def test_category_conflict_never_escalates_to_operation(settings):
    model = Fallback(Intent(kind="operation", target="blog/web", action=Action.RESTART))
    router = make_router(settings, lambda _: httpx.Response(200, json=reply("query")), model)
    assert (await router.route("不要重启，只看状态", {}, [])).kind == "clarify"
    assert router.metrics[-1]["route_path"] == "category_conflict"
    await router.close()


@pytest.mark.asyncio
async def test_fallback_failure_not_retried(settings):
    class Broken(Fallback):
        async def route(self, *args):
            self.calls.append(args)
            raise VeyError("model_failed", "unavailable")

    model = Broken()
    router = make_router(settings, lambda _: httpx.Response(429), model)
    with pytest.raises(VeyError, match="unavailable"):
        await router.route("test", {}, [])
    assert len(model.calls) == 1
    await router.close()


def test_disabled_mode_constructs_no_jev_client(settings, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled must not create a client")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    assert build_router(settings, Fallback()) is None


@pytest.mark.asyncio
async def test_real_core_rule_bypass_and_operation_confirmation(
    core, settings, backend, db_factory
):
    model = Fallback(Intent(kind="operation", target="blog/web", action=Action.RESTART))
    router = make_router(settings, lambda _: httpx.Response(200, json=reply("operation")), model)
    core.model, core.router = model, router
    first = core.accept("rule", "owner", "重启 博客")["task_id"]
    await core.process(first)
    assert not model.calls and not router.metrics and not backend.actions
    assert core.task(first)["status"] == "awaiting_confirmation"
    second = core.accept("jev", "owner", "请把博客服务重新启动一次")["task_id"]
    await core.process(second)
    assert core.task(second)["status"] == "awaiting_confirmation"
    assert not backend.actions
    with db_factory() as db:
        metrics = list(
            db.scalars(select(Event).where(Event.task_id == second, Event.kind == "model"))
        )
        assert {e.data["provider"] for e in metrics} == {"typesafe", "deepseek"}
        assert len(metrics) == 2
    third = core.accept("protected", "owner", "请重新启动数据库")["task_id"]
    model.intent = Intent(kind="operation", target="infra/postgres", action=Action.RESTART)
    await core.process(third)
    assert core.task(third)["status"] == "failed"
    assert not backend.actions
    await router.close()


def evaluation_dataset():
    dataset, _ = load_dataset(Path("evals/datasets/phase2-v1.json"))
    raw = dataset.model_dump()
    raw["cases"] = [
        dict(
            id=f"jev-{i}",
            family=f"jev-{i}",
            split="dev",
            category="query",
            stage="route",
            message=f"展示我的管理范围内全部服务吧 {i}",
            context={},
            expected={"any_of": [{"kind": "query", "tool": {"name": "services"}}]},
            rationale="transport fixture, not real model quality",
        )
        for i in range(3)
    ]
    return Dataset.model_validate(raw)


@pytest.mark.asyncio
async def test_eval_reserves_two_calls_and_counts_both_providers(settings, tmp_path):
    settings.jev_key = SecretStr("test")

    def factory():
        return JevEvaluationProvider(
            settings,
            Fallback(),
            httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(429))),
        )

    report = await run_evaluation(
        evaluation_dataset(),
        "fixture",
        split="dev",
        strategy="jev",
        mode="live",
        output=tmp_path / "eval",
        max_model_calls=3,
        provider_factory=factory,
    )
    assert report["attempted_model_calls"] == 2
    assert report["summary"]["model_calls"] == 2
    assert report["summary"]["not_run"] == 2
    assert report["summary"]["provider_calls"] == {"typesafe": 1, "deepseek": 1}
    assert report["summary"]["estimated_cost"] is None


@pytest.mark.asyncio
async def test_eval_direct_saves_call_budget(settings, tmp_path):
    settings.jev_key = SecretStr("test")

    def factory():
        return JevEvaluationProvider(
            settings,
            Fallback(),
            httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, json=reply("services")))
            ),
        )

    report = await run_evaluation(
        evaluation_dataset(),
        "fixture",
        split="dev",
        strategy="jev",
        mode="live",
        output=tmp_path / "eval",
        max_model_calls=3,
        provider_factory=factory,
    )
    assert report["attempted_model_calls"] == report["summary"]["model_calls"] == 2
    assert report["summary"]["passed"] == 2  # last remaining slot cannot cover fallback
    assert report["summary"]["not_run"] == 1


def test_mixed_pricing_requires_both_models_and_same_currency():
    base = dict(
        provider="fixture",
        model="ds",
        currency="CNY",
        as_of="2026-10-07",
        source="test only",
        input_per_million=1,
        output_per_million=1,
    )
    ds, jev = Pricing(**base), Pricing(**{**base, "model": "jev"})
    metrics = [
        {
            "provider": provider,
            "model": model,
            "usage": {"prompt_tokens": 1000, "completion_tokens": 1000},
        }
        for provider, model in [("deepseek", "ds"), ("typesafe", "jev")]
    ]
    assert mixed_cost(metrics, ds, jev) == 0.004
    assert mixed_cost(metrics, ds, None) is None
    assert mixed_cost(metrics, ds, jev.model_copy(update={"currency": "USD"})) is None
