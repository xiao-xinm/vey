"""Explicit, append-only experiment archive in a dedicated PostgreSQL database."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import psycopg
from psycopg import sql

from vey.domain import digest
from vey.security import safe_value


def connect_archive(url):
    db = psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://"), connect_timeout=5)
    try:
        name = db.execute("SELECT current_database()").fetchone()[0]
        if name != "vey_eval" and not name.startswith("vey_test_eval"):
            raise ValueError("Archive requires vey_eval or vey_test_eval* database")
        if db.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname IN ('vey_core','vey_exec')"
        ).fetchone():
            raise ValueError("Refusing archive in a runtime database")
        return db
    except Exception:
        db.close()
        raise


def initialize(db, writer_role=None):
    with db.transaction():
        db.execute("SELECT pg_advisory_xact_lock(7265402)")
        db.execute("CREATE SCHEMA IF NOT EXISTS vey_eval")
        db.execute("REVOKE ALL ON SCHEMA vey_eval FROM PUBLIC")
        db.execute("""CREATE TABLE IF NOT EXISTS vey_eval.runs (
            run_id varchar(32) PRIMARY KEY,
            schema_version integer NOT NULL CHECK (schema_version = 1),
            report_hash varchar(64) NOT NULL,
            scope text NOT NULL,
            dataset_hash varchar(64) NOT NULL,
            report jsonb NOT NULL,
            archived_at timestamptz NOT NULL DEFAULT now()
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS vey_eval.samples (
            run_id varchar(32) NOT NULL REFERENCES vey_eval.runs(run_id),
            case_id text NOT NULL,
            repetition integer NOT NULL,
            status text NOT NULL,
            result jsonb NOT NULL,
            PRIMARY KEY (run_id, case_id, repetition)
        )""")
        if writer_role:
            role = sql.Identifier(writer_role)
            db.execute(sql.SQL("GRANT USAGE ON SCHEMA vey_eval TO {}").format(role))
            db.execute(
                sql.SQL("GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA vey_eval TO {}").format(role)
            )


def archive_report(db, report):
    # Only normalized, sanitized reports are persisted; never copy key files or .env.
    report = safe_value(report)
    if not re.fullmatch(r"[a-f0-9]{32}", report.get("run_id", "")):
        raise ValueError("Invalid run ID")
    if report.get("scope") not in {"routing_and_next_step_contracts", "full_readonly_diagnosis"}:
        raise ValueError("Unsupported report scope")
    for key in ("dataset_hash", "implementation_hash"):
        if not re.fullmatch(r"[a-f0-9]{64}", report.get(key, "")):
            raise ValueError("Missing provenance hash")
    if report.get("status") not in {
        "completed",
        "interrupted",
        "error",
        "budget_exhausted",
        "partial",
    }:
        raise ValueError("Only finished/partial reports may be archived")
    samples = report.get("results")
    if not isinstance(samples, list):
        raise ValueError("Missing samples")
    keys = [(r["case_id"], r.get("repeat", 1)) for r in samples]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate sample keys")
    fingerprint = digest(report)
    payload = json.dumps(report, ensure_ascii=False)
    if len(payload.encode()) > 10_000_000:
        raise ValueError("Report exceeds 10 MB archive limit")
    with db.transaction():
        row = db.execute(
            "INSERT INTO vey_eval.runs (run_id,schema_version,report_hash,scope,dataset_hash,report) VALUES (%s,1,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING RETURNING run_id",
            (report["run_id"], fingerprint, report["scope"], report["dataset_hash"], payload),
        ).fetchone()
        if not row:
            existing = db.execute(
                "SELECT report_hash FROM vey_eval.runs WHERE run_id=%s", (report["run_id"],)
            ).fetchone()
            if not existing or existing[0] != fingerprint:
                raise ValueError("Run ID already exists with a different immutable report")
            return {"run_id": report["run_id"], "inserted": False, "samples": len(samples)}
        for sample in samples:
            db.execute(
                "INSERT INTO vey_eval.samples VALUES (%s,%s,%s,%s,%s::jsonb)",
                (
                    report["run_id"],
                    sample["case_id"],
                    sample.get("repeat", 1),
                    sample["status"],
                    json.dumps(sample, ensure_ascii=False),
                ),
            )
    return {"run_id": report["run_id"], "inserted": True, "samples": len(samples)}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Dedicated PostgreSQL evaluation archive; never auto-load production credentials"
    )
    parser.add_argument("command", choices=["init", "import", "list"])
    parser.add_argument("--url-file", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--writer-role")
    args = parser.parse_args(argv)
    try:
        with connect_archive(args.url_file.read_text(encoding="utf-8").strip()) as db:
            if args.command == "init":
                initialize(db, args.writer_role)
                print("Archive schema v1 initialized")
            elif args.command == "import":
                if not args.report or args.report.stat().st_size > 10_000_000:
                    raise ValueError("Explicit report <=10MB required")
                print(
                    json.dumps(
                        archive_report(db, json.loads(args.report.read_text(encoding="utf-8")))
                    )
                )
            else:
                rows = db.execute(
                    "SELECT run_id,scope,dataset_hash,archived_at FROM vey_eval.runs ORDER BY archived_at DESC LIMIT 50"
                ).fetchall()
                print(json.dumps(rows, default=str))
        return 0
    except (ValueError, KeyError, TypeError, OSError, psycopg.Error) as error:
        parser.error(
            f"Archive failed ({type(error).__name__}); check dedicated database, permissions and report"
        )


if __name__ == "__main__":
    raise SystemExit(main())
