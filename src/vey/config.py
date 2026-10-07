from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from vey.domain import StrictModel


class Service(StrictModel):
    key: str = Field(pattern=r"^[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+$")
    aliases: list[str] = Field(default_factory=list)
    protected: bool = False
    health_url: str | None = None

    @model_validator(mode="after")
    def check_health(self):
        if self.health_url:
            url = urlsplit(self.health_url)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username:
                raise ValueError("health_url 必须是管理员配置的 HTTP(S) 地址且不含凭据")
        return self


class Thresholds(StrictModel):
    cpu_percent: float = Field(default=90, gt=0, le=100)
    memory_percent: float = Field(default=85, gt=0, le=100)
    disk_percent: float = Field(default=90, gt=0, le=100)
    load_per_cpu: float = Field(default=2, gt=0, le=100)


class Policy(StrictModel):
    owner_id: str = Field(min_length=1)
    self_project: str = Field(min_length=1)
    protected_projects: list[str] = Field(min_length=1)
    protected_container_ids: list[str] = Field(default_factory=list)
    services: list[Service] = Field(min_length=1)
    database_protection_acknowledged: bool = False
    thresholds: Thresholds = Field(default_factory=Thresholds)

    @model_validator(mode="after")
    def validate_policy(self):
        if self.self_project not in self.protected_projects:
            raise ValueError("Agent 自身 Compose 项目必须受保护")
        if len({s.key for s in self.services}) != len(self.services):
            raise ValueError("服务 key 不得重复")
        if not self.database_protection_acknowledged:
            raise ValueError("请登记现有 PostgreSQL 的项目或容器 ID 并确认保护配置")
        if (
            not any(s.protected for s in self.services)
            and not self.protected_container_ids
            and len(self.protected_projects) < 2
        ):
            raise ValueError("必须登记外部 PostgreSQL 依赖的保护对象")
        return self

    def resolve(self, name: str) -> Service:
        from vey.domain import VeyError

        matches = [s for s in self.services if name == s.key or name in s.aliases]
        if len(matches) != 1:
            raise VeyError(
                "ambiguous_target",
                "请使用明确的项目/服务名：" + ", ".join(s.key for s in matches or self.services),
            )
        return matches[0]

    def catalog(self) -> list[dict]:
        return [
            {
                "key": s.key,
                "aliases": s.aliases,
                "protected": s.protected or s.key.split("/")[0] in self.protected_projects,
            }
            for s in self.services
        ]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VEY_", env_file=".env", extra="ignore")
    config: Path = Path("config/vey.toml")
    database_url: SecretStr
    executor_token: SecretStr = SecretStr("")
    debug_token: SecretStr = SecretStr("")
    debug_enabled: bool = False
    executor_socket: str = "/run/vey/executor.sock"
    model_base_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-flash"
    model_key: SecretStr = SecretStr("")
    model_timeout: float = Field(default=20, gt=0, le=30)
    router_mode: Literal["hybrid", "jev"] = "hybrid"
    jev_key: SecretStr = SecretStr("")
    jev_key_file: Path | None = None
    jev_model: str = Field(default="jev-latest", min_length=1, max_length=100)
    jev_timeout: float = Field(default=3, gt=0, le=10)
    jev_min_confidence: float = Field(default=0.85, ge=0, le=1, allow_inf_nan=False)
    wecom_corp_id: str = ""
    wecom_agent_id: int = 0
    wecom_secret: SecretStr = SecretStr("")
    wecom_token: SecretStr = SecretStr("")
    wecom_aes_key: SecretStr = SecretStr("")
    executor_token_file: Path | None = None
    debug_token_file: Path | None = None
    model_key_file: Path | None = None
    wecom_secret_file: Path | None = None
    wecom_token_file: Path | None = None
    wecom_aes_key_file: Path | None = None
    worker_enabled: bool = True
    host_proc: Path | None = None
    host_disk_paths: list[str] = Field(default_factory=list)
    host_disk_labels: dict[str, str] = Field(default_factory=dict)
    docker_timeout: int = Field(default=12, ge=1, le=20)
    log_scan_bytes: int = Field(default=1_000_000, ge=10000, le=2_000_000)
    log_scan_lines: int = Field(default=5000, ge=500, le=10000)
    policy_management_enabled: bool = False
    policy_management_token_file: Path | None = None

    @model_validator(mode="after")
    def validate_settings(self):
        if not self.database_url.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("仅支持 postgresql+psycopg 数据库 URL")
        # Credentials may be mounted as files instead of environment values.
        for name in (
            "executor_token",
            "debug_token",
            "model_key",
            "jev_key",
            "wecom_secret",
            "wecom_token",
            "wecom_aes_key",
        ):
            secret_file = getattr(self, name + "_file")
            if secret_file:
                setattr(self, name, SecretStr(Path(secret_file).read_text().strip()))
        if len(self.executor_token.get_secret_value()) < 32:
            raise ValueError("executor_token 至少需要 32 个字符")
        if self.debug_enabled and len(self.debug_token.get_secret_value()) < 32:
            raise ValueError("本地调试启用时必须配置独立的 32 字符令牌")
        return self

    def policy(self) -> Policy:
        with self.config.open("rb") as file:
            return Policy.model_validate(tomllib.load(file))
