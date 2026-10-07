"""Read sanitized maintenance reports. No access to backups, Docker or admin credentials."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError


class Filesystem(BaseModel):
    total: int = Field(ge=0)
    used: int = Field(ge=0)
    free: int = Field(ge=0)


class Relation(BaseModel):
    schema_name: str = Field(alias="schema", max_length=63)
    name: str = Field(max_length=63)
    table_bytes: int = Field(ge=0)
    index_bytes: int = Field(ge=0)
    total_bytes: int = Field(ge=0)


class DatabaseReport(BaseModel):
    snapshot_started_at: datetime
    database_bytes: int = Field(ge=0)
    dump_bytes: int = Field(ge=0)
    tables: int = Field(ge=0)
    views: int = Field(ge=0)
    data_views_permissions_verified: bool = False
    relations: list[Relation] = Field(default_factory=list, max_length=64)


class Report(BaseModel):
    status: Literal["running", "passed", "failed"]
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    started_at: datetime
    finished_at: datetime | None = None
    stage: str = Field(max_length=80)
    databases: dict[Literal["vey", "vey_eval"], DatabaseReport]
    filesystem_after: Filesystem | None = None
    offsite: bool
    encrypted: bool
    scheduled: bool
    cleanup: Literal["removed", "failed", "identity_mismatch"] | None = None


def read_report(directory: Path, name: str):
    path = directory / name
    try:
        if path.is_symlink():
            return {"state": "invalid"}
        with path.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            return {"state": "invalid"}
        report = Report.model_validate_json(raw)
        if name == "last-success.json" and report.status != "passed":
            return {"state": "invalid"}
        if report.status == "passed" and (
            set(report.databases) != {"vey", "vey_eval"}
            or not all(d.data_views_permissions_verified for d in report.databases.values())
            or report.cleanup != "removed"
            or report.finished_at is None
        ):
            return {"state": "invalid"}
        sampled = report.finished_at or report.started_at
        if sampled.tzinfo is None:
            return {"state": "invalid"}
        age = (datetime.now(UTC) - sampled).total_seconds()
        if age < -60:
            return {"state": "invalid"}
        return {
            "state": "available",
            "stale": age > 86400,
            "running_overdue": report.status == "running" and age > 600,
            "report": report.model_dump(mode="json", by_alias=True),
        }
    except FileNotFoundError:
        return {"state": "missing"}
    except (OSError, ValidationError, json.JSONDecodeError):
        return {"state": "invalid"}


def operations_status(directory: Path | None):
    if directory is None:
        return {"configured": False}
    return {
        "configured": True,
        "latest": read_report(directory, "latest.json"),
        "last_success": read_report(directory, "last-success.json"),
    }
