from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from vey.config import Policy
from vey.domain import StrictModel, digest


class Expectation(StrictModel):
    # Alternatives match subsets of the validated output. Explicit null is an assertion.
    any_of: list[dict] = Field(min_length=1)
    forbid_operation: bool = False
    required_refs: list[str] = Field(default_factory=list)
    required_terms_any: list[list[str]] = Field(default_factory=list)
    forbidden_terms: list[str] = Field(default_factory=list)


class Case(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    family: str = Field(min_length=1)
    split: Literal["dev", "test"]
    category: str = Field(min_length=1)
    stage: Literal["route", "plan"]
    message: str = Field(min_length=1, max_length=4000)
    context: dict = Field(default_factory=dict)
    target: str | None = None
    evidence: list[dict] = Field(default_factory=list)
    expected: Expectation
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence(self):
        ids = [e.get("id") for e in self.evidence]
        if len(set(ids)) != len(ids) or any(not isinstance(i, str) for i in ids):
            raise ValueError("Evidence IDs must be unique strings")
        if not set(self.expected.required_refs).issubset(ids):
            raise ValueError("Expected evidence references must exist in the fixture")
        if self.stage == "route" and self.evidence:
            raise ValueError("Routing fixtures use context, not planner evidence")
        if any(not choice for choice in self.expected.any_of):
            raise ValueError("Empty output assertions would match every response")
        return self


class Dataset(StrictModel):
    version: str
    scope: Literal["routing_and_next_step_contracts"]
    policy: Policy
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_splits(self):
        ids, families, inputs = set(), {}, set()
        targets = {s.key for s in self.policy.services}
        for case in self.cases:
            if case.id in ids:
                raise ValueError("Duplicate case ID")
            ids.add(case.id)
            if case.family in families and families[case.family] != case.split:
                raise ValueError("A scenario family cannot span dev and test")
            families[case.family] = case.split
            fingerprint = digest(
                [case.stage, case.message.strip(), case.context, case.target, case.evidence]
            )
            if fingerprint in inputs:
                raise ValueError("Duplicate case input")
            inputs.add(fingerprint)
            if case.target and case.target not in targets:
                raise ValueError("Planner target must be in the synthetic catalog")
        return self


class Pricing(StrictModel):
    provider: str
    model: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    as_of: date
    source: str = Field(min_length=1)
    input_per_million: float = Field(ge=0, allow_inf_nan=False)
    output_per_million: float = Field(ge=0, allow_inf_nan=False)
    cached_input_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)


def load_dataset(path: Path) -> tuple[Dataset, str]:
    if path.stat().st_size > 2_000_000:
        raise ValueError("Dataset exceeds the 2 MB evaluation limit")
    raw = json.loads(path.read_text(encoding="utf-8"))
    dataset = Dataset.model_validate(raw)
    return dataset, digest(dataset.model_dump(mode="json"))
