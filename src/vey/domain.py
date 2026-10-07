from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


class VeyError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Action(StrEnum):
    START = "start"
    STOP = "stop"
    RESTART = "restart"


class ToolCall(StrictModel):
    name: Literal["system", "services", "inspect", "stats", "ranking", "logs", "health"] = Field(
        description="system=宿主机CPU/内存/磁盘；services=服务列表；inspect=指定服务容器状态；stats=指定服务容器资源；ranking=已配置容器资源排行，无需target；logs=指定服务日志；health=指定服务业务健康检查"
    )
    target: str | None = Field(
        default=None,
        max_length=120,
        description="catalog中的项目/服务key；inspect、stats、health必须提供，logs首次查询必须提供；system和services不需要target，禁止虚构宿主机为容器目标",
    )
    lines: int = Field(default=100, ge=1, le=500, strict=True)
    keyword: str | None = Field(default=None, max_length=100)
    since: datetime | None = None
    until: datetime | None = None
    cursor: str | None = Field(default=None, max_length=64)
    sort_by: Literal["memory", "cpu"] = "memory"

    @model_validator(mode="after")
    def check_dates(self):
        for value in (self.since, self.until):
            if value is not None and value.tzinfo is None:
                raise ValueError("日志时间必须包含时区")
        if self.since and self.until and self.since >= self.until:
            raise ValueError("开始时间必须早于结束时间")
        if (
            self.name not in ("system", "services", "ranking")
            and not self.target
            and not self.cursor
        ):
            raise ValueError("此工具需要明确目标")
        if self.cursor and self.name != "logs":
            raise ValueError("只有日志工具支持游标")
        return self


class Intent(StrictModel):
    kind: Literal["query", "diagnose", "operation", "clarify"]
    target: str | None = Field(default=None, max_length=120)
    action: Action | None = None
    tool: ToolCall | None = None
    message: str = Field(default="", max_length=1500)

    @model_validator(mode="after")
    def check_intent(self):
        if self.kind == "operation" and (not self.target or not self.action):
            raise ValueError("操作必须包含目标和动作")
        if self.kind != "operation" and self.action is not None:
            raise ValueError("查询不能包含修改动作")
        return self


class Hypothesis(StrictModel):
    code: Literal[
        "container_exit",
        "dependency_unavailable",
        "configuration_error",
        "resource_pressure",
        "application_error",
        "unknown",
    ]
    statement: str = Field(min_length=1, max_length=500)
    confidence: Literal["possible", "supported"]
    evidence_refs: list[str] = Field(min_length=1, max_length=5)
    temporal_scope: Literal["current", "historical", "unknown"] = "unknown"
    polarity: Literal["present", "absent", "unknown"] = "unknown"


class Verification(StrictModel):
    question: str = Field(min_length=1, max_length=300)
    tool: ToolCall


class Diagnosis(StrictModel):
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=5)
    uncertainty: str = Field(min_length=1, max_length=600)
    verification: list[Verification] = Field(default_factory=list, max_length=3)


class NextStep(StrictModel):
    tool: ToolCall | None = None
    summary: str = Field(default="", max_length=3000)
    diagnosis: Diagnosis | None = None


class Actor(StrictModel):
    user_id: str = Field(min_length=1, max_length=128)
    generation: str = Field(min_length=1, max_length=64)
    expires_at: datetime
    started_at: datetime


class ReadRequest(StrictModel):
    actor: Actor
    call: ToolCall
    policy_revision: str | None = Field(default=None, max_length=64)


class PrepareRequest(StrictModel):
    actor: Actor
    task_id: str
    target: str
    action: Action
    policy_revision: str | None = Field(default=None, max_length=64)


class ConfirmRequest(StrictModel):
    actor: Actor
    code: str = Field(pattern=r"^[A-F0-9]{8}$")


class DebugMessage(StrictModel):
    message_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4000)
