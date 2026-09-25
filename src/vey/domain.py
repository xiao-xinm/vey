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
    name: Literal["system", "services", "inspect", "stats", "logs", "health"]
    target: str | None = Field(default=None, max_length=120)
    lines: int = Field(default=100, ge=1, le=500, strict=True)
    keyword: str | None = Field(default=None, max_length=100)
    since: datetime | None = None
    until: datetime | None = None
    cursor: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def check_dates(self):
        for value in (self.since, self.until):
            if value is not None and value.tzinfo is None:
                raise ValueError("日志时间必须包含时区")
        if self.since and self.until and self.since >= self.until:
            raise ValueError("开始时间必须早于结束时间")
        if self.name not in ("system", "services") and not self.target and not self.cursor:
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


class NextStep(StrictModel):
    tool: ToolCall | None = None
    summary: str = Field(default="", max_length=3000)


class Actor(StrictModel):
    user_id: str = Field(min_length=1, max_length=128)
    generation: str = Field(min_length=1, max_length=64)
    expires_at: datetime
    started_at: datetime


class ReadRequest(StrictModel):
    actor: Actor
    call: ToolCall


class PrepareRequest(StrictModel):
    actor: Actor
    task_id: str
    target: str
    action: Action


class ConfirmRequest(StrictModel):
    actor: Actor
    code: str = Field(pattern=r"^[A-F0-9]{8}$")


class DebugMessage(StrictModel):
    message_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4000)
