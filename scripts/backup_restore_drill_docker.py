"""Back up a dedicated Vey database and verify restoration in a temporary database.

Run on the Docker host. Stop Vey writers first; leave the PostgreSQL container up.
Uses the container's existing local PostgreSQL authentication, without reading passwords.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--database", default="vey")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.database != "vey" and not args.database.startswith("vey_test"):
        raise SystemExit("Only a dedicated vey/vey_test* source is allowed")
    if not re.fullmatch(r"[a-zA-Z0-9_]+", args.database):
        raise SystemExit("Invalid database name")
    os.umask(0o077)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    def psql(database, query):
        return subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                args.container,
                "sh",
                "-c",
                'exec psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-postgres}" -d "$1" -At',
                "sh",
                database,
            ],
            input=query,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    def fingerprint(database):
        tables = psql(
            database,
            "SELECT schemaname || '.' || tablename FROM pg_tables WHERE schemaname IN ('vey_core', 'vey_exec') ORDER BY 1;",
        ).splitlines()
        result = {}
        for table in tables:
            if not re.fullmatch(r"vey_(?:core|exec)\.[a-z_]+", table):
                raise RuntimeError("Unexpected table identifier")
            data = psql(
                database, f"SELECT row_to_json(t) FROM {table} t ORDER BY row_to_json(t)::text;"
            )
            count = int(psql(database, f"SELECT count(*) FROM {table};"))
            result[table] = {"rows": count, "sha256": hashlib.sha256(data.encode()).hexdigest()}
        if len(tables) < 7:
            raise RuntimeError("Vey schema is incomplete")
        result["migration"] = psql(database, "SELECT version_num FROM alembic_version;")
        return result

    before = fingerprint(args.database)
    # The Docker host may still use Ubuntu 22.04's Python 3.10.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")  # noqa: UP017
    dump = args.output_dir / f"{args.database}-{stamp}.dump"
    with dump.open("xb") as output:
        subprocess.run(
            [
                "docker",
                "exec",
                args.container,
                "sh",
                "-c",
                'exec pg_dump -U "${POSTGRES_USER:-postgres}" -d "$1" -Fc',
                "sh",
                args.database,
            ],
            stdout=output,
            stderr=subprocess.PIPE,
            check=True,
        )
    target = "vey_test_restore_" + uuid.uuid4().hex
    psql("postgres", f"CREATE DATABASE {target};")
    try:
        with dump.open("rb") as input_file:
            subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    args.container,
                    "sh",
                    "-c",
                    'exec pg_restore -U "${POSTGRES_USER:-postgres}" -d "$1" --no-owner --no-privileges --exit-on-error',
                    "sh",
                    target,
                ],
                stdin=input_file,
                capture_output=True,
                check=True,
            )
        after = fingerprint(target)
        if before != after:
            raise RuntimeError("Restored data differs; ensure Vey writers were stopped")
        report = {
            "status": "passed",
            "database": args.database,
            "at": stamp,
            "dump": str(dump),
            "dump_bytes": dump.stat().st_size,
            "data": after,
            "scope": "table contents and migration version; runtime grants must be restored separately",
        }
        (args.output_dir / f"report-{stamp}.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "status": "passed",
                    "tables_verified": len(after) - 1,
                    "migration": after["migration"],
                    "dump": str(dump),
                    "dump_bytes": dump.stat().st_size,
                }
            )
        )
    finally:
        # This database name was generated and created in this invocation only.
        psql("postgres", f"DROP DATABASE {target};")


if __name__ == "__main__":
    main()
