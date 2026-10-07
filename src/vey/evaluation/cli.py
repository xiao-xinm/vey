from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from pydantic import SecretStr

from vey.evaluation.runner import compare_reports, run_evaluation
from vey.evaluation.schema import Pricing, load_dataset
from vey.evaluation.trajectory import load_trajectories, run_trajectories
from vey.jev import JevEvaluationProvider
from vey.model import DeepSeekProvider


def live_provider(args):
    url = urlsplit(args.base_url)
    if url.scheme != "https" or not url.hostname or url.username or url.query or url.fragment:
        raise ValueError("Explicit HTTPS model endpoint required")
    if not args.key_file:
        raise ValueError("Explicit model key file required")
    key = args.key_file.read_text(encoding="utf-8").strip()
    if not key:
        raise ValueError("Empty key")
    settings = SimpleNamespace(
        model_key=SecretStr(key),
        model_name=args.model,
        model_base_url=args.base_url,
        model_timeout=min(args.timeout, 30),
    )
    if getattr(args, "strategy", None) == "jev":
        if not args.jev_key_file:
            raise ValueError("Explicit Jev key file required")
        jev_key = args.jev_key_file.read_text(encoding="utf-8").strip()
        if not jev_key or not 0 < args.jev_timeout <= 10 or not 0 <= args.jev_min_confidence <= 1:
            raise ValueError("Invalid Jev credentials or bounds")
        settings.jev_key = SecretStr(jev_key)
        settings.jev_model = args.jev_model
        settings.jev_timeout = args.jev_timeout
        settings.jev_min_confidence = args.jev_min_confidence
        return lambda: JevEvaluationProvider(settings, DeepSeekProvider(settings))
    return lambda: DeepSeekProvider(settings)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Evaluate synthetic fixtures without Docker or a database"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser(
        "validate", help="Validate cases and partition isolation without model calls"
    )
    validate.add_argument("dataset", type=Path)
    compare = sub.add_parser(
        "compare", help="Compare paired live runs on the same dataset and split"
    )
    compare.add_argument("left", type=Path)
    compare.add_argument("right", type=Path)
    compare.add_argument("--output", type=Path, required=True)
    trajectory = sub.add_parser(
        "trajectory", help="Run the shared diagnosis loop on offline tool fixtures"
    )
    trajectory.add_argument("dataset", type=Path)
    trajectory.add_argument("--output", type=Path, required=True)
    trajectory.add_argument("--mode", choices=["scripted", "live"], default="scripted")
    trajectory.add_argument("--key-file", type=Path)
    trajectory.add_argument("--model", default="deepseek-flash")
    trajectory.add_argument("--base-url", default="https://api.deepseek.com")
    trajectory.add_argument("--max-model-calls", type=int, default=100)
    trajectory.add_argument("--timeout", type=float, default=60)
    run = sub.add_parser("run")
    run.add_argument("dataset", type=Path)
    run.add_argument("--split", choices=["dev", "test"], required=True)
    run.add_argument("--strategy", choices=["hybrid", "model", "jev"], default="hybrid")
    run.add_argument("--mode", choices=["rules", "live"], default="rules")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--repeats", type=int, choices=range(1, 6), default=1)
    run.add_argument("--max-model-calls", type=int, default=100)
    run.add_argument("--limit", type=int)
    run.add_argument("--timeout", type=float, default=25)
    run.add_argument("--model", default="deepseek-flash")
    run.add_argument("--base-url", default="https://api.deepseek.com")
    run.add_argument("--key-file", type=Path)
    run.add_argument("--jev-key-file", type=Path)
    run.add_argument("--jev-model", default="jev-latest")
    run.add_argument("--jev-timeout", type=float, default=3)
    run.add_argument("--jev-min-confidence", type=float, default=0.85)
    run.add_argument("--jev-pricing", type=Path)
    run.add_argument(
        "--pricing", type=Path, help="Optional versioned price basis, never a claimed bill"
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "compare":
            result = compare_reports(
                json.loads(args.left.read_text(encoding="utf-8")),
                json.loads(args.right.read_text(encoding="utf-8")),
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as file:
                json.dump(result, file, ensure_ascii=False, indent=2)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "trajectory":
            dataset = load_trajectories(args.dataset)
            factory = None
            if args.mode == "live":
                factory = live_provider(args)
            report = asyncio.run(
                run_trajectories(
                    dataset,
                    args.output,
                    provider_factory=factory,
                    max_model_calls=args.max_model_calls,
                    timeout=args.timeout,
                )
            )
            print(
                json.dumps(
                    {
                        k: report[k]
                        for k in ("run_id", "status", "executed", "passed", "model_calls")
                    }
                )
            )
            return (
                0
                if report["status"] == "completed" and report["passed"] == report["executed"]
                else 1
            )
        dataset, fingerprint = load_dataset(args.dataset)
        if args.command == "validate":
            print(
                json.dumps(
                    {
                        "version": dataset.version,
                        "hash": fingerprint,
                        "cases": len(dataset.cases),
                        "splits": {
                            s: sum(c.split == s for c in dataset.cases) for s in ("dev", "test")
                        },
                    }
                )
            )
            return 0
        if not 0 <= args.max_model_calls <= 500 or not 1 <= args.timeout <= 60:
            parser.error("request budget must be 0..500; timeout must be 1..60 seconds")
        pricing = (
            Pricing.model_validate_json(args.pricing.read_text(encoding="utf-8"))
            if args.pricing
            else None
        )
        provider_factory = None
        if args.mode == "live":
            provider_factory = live_provider(args)

        report = asyncio.run(
            run_evaluation(
                dataset,
                fingerprint,
                split=args.split,
                strategy=args.strategy,
                mode=args.mode,
                output=args.output,
                provider_factory=provider_factory,
                model_name=args.model,
                repeats=args.repeats,
                max_model_calls=args.max_model_calls,
                timeout=args.timeout,
                limit=args.limit,
                pricing=pricing,
                jev_pricing=Pricing.model_validate_json(
                    args.jev_pricing.read_text(encoding="utf-8")
                )
                if args.jev_pricing
                else None,
                routing_config={
                    "model": args.jev_model,
                    "timeout": args.jev_timeout,
                    "min_confidence": args.jev_min_confidence,
                }
                if args.strategy == "jev"
                else None,
                progress=lambda row: print(json.dumps(row, ensure_ascii=False), flush=True),
            )
        )
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        if report["summary"]["failed"]:
            return 1
        # Rules mode deliberately leaves model-dependent cases unexecuted.
        return 2 if args.mode == "live" and report["status"] != "completed" else 0
    except (ValueError, OSError) as exc:
        # ValidationError may include an input value; print only its class.
        parser.error(
            f"evaluation setup failed ({type(exc).__name__}); check dataset, paths and bounds"
        )


if __name__ == "__main__":
    raise SystemExit(main())
