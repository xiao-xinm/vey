"""Restore drill for an isolated vey_test* database; never starts the Agent."""

import argparse
import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url


def fingerprint(connection):
    tables = connection.execute(
        "SELECT schemaname, tablename FROM pg_tables WHERE schemaname IN ('vey_core', 'vey_exec') ORDER BY 1, 2"
    ).fetchall()
    result = {}
    for schema, table in tables:
        rows = connection.execute(
            sql.SQL("SELECT row_to_json(t) FROM {}.{} t").format(
                sql.Identifier(schema), sql.Identifier(table)
            )
        ).fetchall()
        values = sorted(json.dumps(row[0], sort_keys=True, ensure_ascii=False) for row in rows)
        result[f"{schema}.{table}"] = {
            "rows": len(rows),
            "sha256": hashlib.sha256("\n".join(values).encode()).hexdigest(),
        }
    result["migration"] = connection.execute("SELECT version_num FROM alembic_version").fetchone()[
        0
    ]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-bin", type=Path, required=True)
    args = parser.parse_args()
    url = make_url(os.environ["VEY_TEST_DATABASE_URL"])
    if not url.database or not url.database.startswith("vey_test"):
        raise SystemExit("Only an isolated vey_test* source is allowed")
    env = {
        **os.environ,
        "PGHOST": url.host or "localhost",
        "PGPORT": str(url.port or 5432),
        "PGUSER": url.username,
        "PGPASSWORD": url.password or "",
    }
    connect = {
        "host": env["PGHOST"],
        "port": env["PGPORT"],
        "user": env["PGUSER"],
        "password": env["PGPASSWORD"],
    }
    target = "vey_test_restore_" + uuid.uuid4().hex
    directory = Path(".local/backup-drill")
    directory.mkdir(parents=True, exist_ok=True)
    dump = directory / "vey_test.dump"
    suffix = ".exe" if os.name == "nt" else ""
    with psycopg.connect(**connect, dbname=url.database) as source:
        before = fingerprint(source)
    subprocess.run(
        [str(args.pg_bin / ("pg_dump" + suffix)), "-d", url.database, "-Fc", "-f", str(dump)],
        env=env,
        check=True,
        capture_output=True,
    )
    with psycopg.connect(**connect, dbname="postgres", autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))
        try:
            subprocess.run(
                [
                    str(args.pg_bin / ("pg_restore" + suffix)),
                    "-d",
                    target,
                    "--no-owner",
                    "--no-privileges",
                    "--exit-on-error",
                    str(dump),
                ],
                env=env,
                check=True,
                capture_output=True,
            )
            with psycopg.connect(**connect, dbname=target) as restored:
                after = fingerprint(restored)
            if before != after:
                raise RuntimeError("Restored contents differ from source snapshot")
            report = {
                "status": "passed",
                "source": url.database,
                "temporary_target": target,
                "data": after,
                "dump_bytes": dump.stat().st_size,
            }
            (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(
                f"Backup restore verified: {len(after) - 1} tables, migration {after['migration']}; row counts and content hashes match."
            )
        finally:
            # Only the random database created by this invocation is removed.
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(target)))


if __name__ == "__main__":
    main()
