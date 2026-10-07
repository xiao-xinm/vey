"""Online local backup and network-isolated restore drill. Linux Docker host, Python 3.10+."""

import argparse
import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import tarfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DATABASES = ("vey", "vey_eval")
ROLES = (
    "vey_migrator",
    "vey_core",
    "vey_executor",
    "vey_dashboard",
    "vey_eval_owner",
    "vey_eval_writer",
    "vey_eval_reader",
)
SCHEMAS = "('public','vey_core','vey_exec','vey_dashboard','vey_eval')"


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value):
        raise ValueError("Unsupported SQL identifier")
    return '"' + value + '"'


def utc():
    return datetime.now(timezone.utc).isoformat()  # noqa: UP017


def sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write_json(path, value, mode=0o600):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as output:
        output.write(json.dumps(value, ensure_ascii=False, indent=2))
        output.flush()
        os.fsync(output.fileno())
    temporary.chmod(mode)
    temporary.replace(path)


def psql_command(container, database):
    identifier(database)
    return [
        "docker",
        "exec",
        "-i",
        container,
        "sh",
        "-c",
        'exec psql -X -qAt -v ON_ERROR_STOP=1 -U "${POSTGRES_USER:-postgres}" -d "$1"',
        "sh",
        database,
    ]


def query(container, database, statement, snapshot=None):
    prefix = "SET statement_timeout='60s'; SET timezone='UTC'; SET search_path=public;\n"
    if snapshot:
        if not re.fullmatch(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{8}-[0-9]+", snapshot):
            raise ValueError("Invalid exported snapshot")
        prefix += f"BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET TRANSACTION SNAPSHOT '{snapshot}';\n"
    result = subprocess.run(
        psql_command(container, database),
        input=prefix + statement + ("\nCOMMIT;" if snapshot else ""),
        text=True,
        capture_output=True,
        check=True,
        timeout=90,
    )
    return result.stdout.strip()


@contextmanager
def exported_snapshot(container, database):
    # Keep the exporting transaction alive while pg_dump and fingerprints use the same snapshot.
    with subprocess.Popen(
        psql_command(container, database),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as keeper:
        try:
            keeper.stdin.write(
                "SET idle_in_transaction_session_timeout='180s';\n"
                "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\nSELECT pg_export_snapshot();\n"
            )
            keeper.stdin.flush()
            if not select.select([keeper.stdout], [], [], 15)[0]:
                raise TimeoutError("Snapshot exporter did not respond")
            snapshot = keeper.stdout.readline().strip()
            if not re.fullmatch(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{8}-[0-9]+", snapshot):
                raise ValueError("Snapshot exporter returned an invalid ID")
            yield snapshot
            keeper.stdin.write("ROLLBACK;\n\\q\n")
            keeper.stdin.flush()
            keeper.wait(timeout=10)
            if keeper.returncode:
                raise RuntimeError("Snapshot keeper failed")
        finally:
            if keeper.poll() is None:
                keeper.terminate()
                try:
                    keeper.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    keeper.kill()


def fingerprint(container, database, snapshot=None):
    def sql(statement):
        return query(container, database, statement, snapshot)

    relations = json.loads(
        sql(f"""SELECT coalesce(json_agg(x ORDER BY schema,name),'[]') FROM
        (SELECT n.nspname AS schema,c.relname AS name,c.relkind AS kind,c.reloptions AS options,pg_get_userbyid(c.relowner) AS owner
         FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
         WHERE n.nspname IN {SCHEMAS} AND c.relkind IN ('r','v','S')) x;""")
    )
    if not relations:
        raise ValueError("No Vey relations to verify")
    tables, views = {}, {}
    for relation in relations:
        name = identifier(relation["schema"]) + "." + identifier(relation["name"])
        key = relation["schema"] + "." + relation["name"]
        if relation["kind"] == "r":
            tables[key] = json.loads(
                sql(f"""SELECT json_build_object('rows',count(*),'content_md5',
                md5(coalesce(string_agg(md5(row_to_json(t)::text),'' ORDER BY md5(row_to_json(t)::text)),''))) FROM {name} t;""")
            )
        if relation["kind"] == "v":
            views[key] = sql(f"SELECT pg_get_viewdef('{key}'::regclass,true);")
    privileges = json.loads(
        sql(f"""SELECT coalesce(json_agg(x ORDER BY role,schema,name),'[]') FROM
      (SELECT r.rolname AS role,n.nspname AS schema,c.relname AS name,
        has_schema_privilege(r.oid,n.oid,'USAGE') AS schema_usage,
        has_schema_privilege(r.oid,n.oid,'CREATE') AS schema_create,
        CASE WHEN c.relkind='S' THEN ARRAY[has_sequence_privilege(r.oid,c.oid,'USAGE'),
            has_sequence_privilege(r.oid,c.oid,'SELECT'),has_sequence_privilege(r.oid,c.oid,'UPDATE')]
        ELSE ARRAY[has_table_privilege(r.oid,c.oid,'SELECT'),has_table_privilege(r.oid,c.oid,'INSERT'),
            has_table_privilege(r.oid,c.oid,'UPDATE'),has_table_privilege(r.oid,c.oid,'DELETE'),
            has_table_privilege(r.oid,c.oid,'TRUNCATE'),has_table_privilege(r.oid,c.oid,'REFERENCES'),
            has_table_privilege(r.oid,c.oid,'TRIGGER')] END AS permissions
        FROM pg_roles r CROSS JOIN pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE r.rolname IN ({",".join(repr(r) for r in ROLES)})
          AND n.nspname IN {SCHEMAS} AND c.relkind IN ('r','v','S')) x;""")
    )
    return {"tables": tables, "views": views, "relations": relations, "privileges": privileges}


def canonical_views(container, database, definitions):
    # PostgreSQL may expand implicit casts when restoring pg_dump's view SQL.
    # Reparse each source/target definition in a temporary view in the isolated DB;
    # compare PostgreSQL's own output, never remove casts by text substitution.
    return {
        name: query(
            container,
            database,
            "CREATE TEMP VIEW vey_restore_verify AS "
            + definition
            + "\nSELECT pg_get_viewdef('pg_temp.vey_restore_verify'::regclass,true);",
        )
        for name, definition in definitions.items()
    }


def database_security(container, database):
    return json.loads(
        query(
            container,
            database,
            """SELECT json_build_object('owner',pg_get_userbyid(datdba),
        'acl',(SELECT json_agg(json_build_object('role',CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END,
                'privilege',a.privilege_type,'grantable',a.is_grantable) ORDER BY a.grantee,a.privilege_type)
         FROM aclexplode(coalesce(datacl,acldefault('d',datdba))) a))
        FROM pg_database WHERE datname=current_database();""",
        )
    )


def archive_configuration(source, destination):
    source = source.resolve()
    candidates = [
        ".env",
        "config",
        "secrets",
        "tls",
        "src",
        "scripts",
        "deploy",
        "migrations",
        "pyproject.toml",
        "uv.lock",
        "alembic.ini",
        "Dockerfile",
    ]
    candidates += [p.name for p in source.glob("compose*.yaml")]
    files = []
    for name in candidates:
        path = source / name
        if path.is_symlink():
            raise ValueError("Configuration backup rejects symlinks")
        if not path.exists():
            continue
        for item in [path] if path.is_file() else sorted(path.rglob("*")):
            if item.is_symlink():
                raise ValueError("Configuration backup rejects symlinks")
            if item.is_file():
                if "__pycache__" in item.parts or item.suffix == ".pyc":
                    continue
                item.resolve().relative_to(source)
                files.append(item)
    if not (source / ".env").is_file() or not (source / "config/vey.toml").is_file():
        raise ValueError("Expected dedicated Vey project configuration")
    if sum(p.stat().st_size for p in files) > 100_000_000:
        raise ValueError("Configuration exceeds 100 MB limit")
    hashes = {p.relative_to(source).as_posix(): sha256(p) for p in files}
    with tarfile.open(destination, "x:gz") as archive:
        for p in files:
            archive.add(p, arcname=p.relative_to(source).as_posix(), recursive=False)
    if hashes != {p.relative_to(source).as_posix(): sha256(p) for p in files}:
        raise RuntimeError("Configuration changed during backup; retry without admin changes")
    # Verify archive members by streaming; never unpack production secrets into the temporary DB.
    with tarfile.open(destination, "r:gz") as archive:
        for member in archive:
            if not member.isfile():
                raise ValueError("Unexpected configuration archive member")
            with archive.extractfile(member) as stream:
                if hashlib.sha256(stream.read()).hexdigest() != hashes[member.name]:
                    raise RuntimeError("Configuration archive verification failed")
    return {
        "files": len(hashes),
        "bytes": destination.stat().st_size,
        "sha256": sha256(destination),
    }


def require_isolated(info, created_id, run_id):
    if (
        not created_id
        or info["Id"] != created_id
        or info["Name"] != "/vey-restore-" + run_id[:12]
        or info["HostConfig"]["NetworkMode"] != "none"
        or info["HostConfig"].get("PortBindings")
        or any(m.get("Type") == "bind" for m in info.get("Mounts", []))
        or info["Config"].get("Labels", {}).get("vey.restore-drill") != run_id
    ):
        raise ValueError("Refusing mutation outside this invocation's isolated restore container")


def quarantine(container):
    # Call only after require_isolated; no Agent process is started in the restore container.
    query(
        container,
        "vey",
        """BEGIN;
        TRUNCATE vey_core.sessions,vey_exec.sessions,vey_exec.log_cursors;
        UPDATE vey_core.tasks SET text='',lease_until=NULL;
        UPDATE vey_core.tasks SET status='unknown',result='恢复演练：旧任务未重放，需人工核对'
          WHERE status IN ('queued','control_queued','running','awaiting_confirmation');
        UPDATE vey_exec.grants SET status='cancelled' WHERE status='pending';
        UPDATE vey_exec.grants SET status='unknown' WHERE status='executing';
        UPDATE vey_core.outbox SET status='expired',body='' WHERE status<>'sent';
        UPDATE vey_core.outbox SET body=''; COMMIT;""",
    )
    pending = query(
        container,
        "vey",
        """SELECT
        (SELECT count(*) FROM vey_core.tasks WHERE status IN ('queued','control_queued','running','awaiting_confirmation'))+
        (SELECT count(*) FROM vey_exec.grants WHERE status IN ('pending','executing'))+
        (SELECT count(*) FROM vey_core.outbox WHERE status='pending')+
        (SELECT count(*) FROM vey_core.sessions)+(SELECT count(*) FROM vey_exec.sessions)+
        (SELECT count(*) FROM vey_exec.log_cursors);""",
    )
    if pending != "0":
        raise RuntimeError("Restored state still contains replayable work")
    return {"replayable_records": 0, "agent_started": False, "source_modified": False}


def capacity(container, database):
    return json.loads(
        query(
            container,
            database,
            f"""SELECT coalesce(json_agg(x ORDER BY total_bytes DESC),'[]') FROM
      (SELECT n.nspname AS schema,c.relname AS name,pg_table_size(c.oid) AS table_bytes,
        pg_indexes_size(c.oid) AS index_bytes,pg_total_relation_size(c.oid) AS total_bytes
       FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
       WHERE n.nspname IN {SCHEMAS} AND c.relkind='r') x;""",
        )
    )


def run(args):
    if os.name != "posix":
        raise ValueError("Run on the Linux Docker host")
    import fcntl

    # Host administrator command: serialize invocations so reports and RAM usage stay bounded.
    if os.geteuid() != 0:
        raise ValueError("Run through sudo on the Docker host")
    descriptor = os.open(
        "/run/lock/vey-operations-drill.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return run_locked(args)


def run_locked(args):
    os.umask(0o077)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", args.container):
        raise ValueError("Invalid source container")
    project = args.project.resolve()
    backup_root = args.output_dir.resolve()
    backup_root.mkdir(parents=True, exist_ok=True)
    if backup_root.is_relative_to(project) or project.is_relative_to(backup_root):
        raise ValueError("Use a dedicated backup directory")
    backup_root.chmod(0o700)
    if shutil.disk_usage(backup_root).free < 2_000_000_000:
        raise ValueError("At least 2 GB free space required")
    run_id = uuid.uuid4().hex
    destination = backup_root / run_id
    destination.mkdir(mode=0o700)
    report = {
        "status": "running",
        "run_id": run_id,
        "started_at": utc(),
        "databases": {},
        "offsite": False,
        "encrypted": False,
        "scheduled": False,
        "source_writers_stopped": False,
        "stage": "preflight",
        "filesystem_before": dict(
            zip(("total", "used", "free"), shutil.disk_usage(backup_root), strict=True)
        ),
    }
    report_dir = args.report_dir.resolve()
    if report_dir.is_relative_to(backup_root) or backup_root.is_relative_to(report_dir):
        raise ValueError("Reports must be separated from private backup material")
    report_dir.mkdir(parents=True, exist_ok=True)

    def publish():
        write_json(destination / "report.json", report)
        write_json(report_dir / "latest.json", report, 0o640)
        if os.geteuid() == 0:
            os.chown(report_dir / "latest.json", 0, 10000)

    publish()
    created_id = None
    try:
        source = json.loads(subprocess.check_output(["docker", "inspect", args.container]))[0]
        source_admin = query(args.container, "postgres", "SELECT session_user;")
        identifier(source_admin)
        if source_admin in ROLES:
            raise ValueError("Use the source PostgreSQL administrator")
        report["postgres_image"] = source["Image"]
        report["postgres_version"] = query(args.container, "postgres", "SHOW server_version;")
        roles = json.loads(
            query(
                args.container,
                "postgres",
                f"""SELECT json_agg(json_build_object('name',rolname,'inherit',rolinherit,
            'settings',rolconfig,'unsafe',rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)) FROM pg_roles
            WHERE rolname IN ({",".join(repr(r) for r in ROLES)});""",
            )
        )
        if not roles or {r["name"] for r in roles} != set(ROLES):
            raise ValueError("Expected all seven Vey roles")
        if any(r["unsafe"] for r in roles):
            raise ValueError("Review privileged Vey roles before recovery")
        memberships = query(
            args.container,
            "postgres",
            f"SELECT count(*) FROM pg_auth_members WHERE member IN (SELECT oid FROM pg_roles WHERE rolname IN ({','.join(repr(r) for r in ROLES)}));",
        )
        if memberships != "0":
            raise ValueError("Role memberships require a separate recovery review")
        report["stage"] = "configuration_backup"
        report["configuration"] = archive_configuration(
            project, destination / "configuration.tar.gz"
        )
        expectations = {}
        for database in DATABASES:
            report["stage"] = "backup_" + database
            size = int(
                query(args.container, database, "SELECT pg_database_size(current_database());")
            )
            if size > 128 * 1024 * 1024:
                raise ValueError("Source exceeds this 128 MiB per-database drill limit")
            started = utc()
            with exported_snapshot(args.container, database) as snapshot:
                expected = fingerprint(args.container, database, snapshot)
                security = database_security(args.container, database)
                dump = destination / (database + ".dump")
                with dump.open("xb") as output:
                    subprocess.run(
                        [
                            "docker",
                            "exec",
                            args.container,
                            "sh",
                            "-c",
                            'exec pg_dump -U "${POSTGRES_USER:-postgres}" -d "$1" -Fc --snapshot="$2"',
                            "sh",
                            database,
                            snapshot,
                        ],
                        stdout=output,
                        stderr=subprocess.PIPE,
                        check=True,
                        timeout=120,
                    )
                    output.flush()
                    os.fsync(output.fileno())
                expectations[database] = {"fingerprint": expected, "security": security}
            report["databases"][database] = {
                "snapshot_started_at": started,
                "dump_bytes": dump.stat().st_size,
                "dump_sha256": sha256(dump),
                "database_bytes": size,
                "tables": len(expected["tables"]),
                "views": len(expected["views"]),
                "rows": {name: value["rows"] for name, value in expected["tables"].items()},
                "relations": capacity(args.container, database),
            }
        write_json(
            destination / "restore-manifest.json",
            {"roles": roles, "databases": expectations, "bootstrap_admin": source_admin},
        )
        # Same image digest as source. Isolated temporary filesystems, no host paths, ports or Docker socket.
        report["stage"] = "isolated_postgres_start"
        name = "vey-restore-" + run_id[:12]
        uid = subprocess.check_output(
            ["docker", "exec", args.container, "id", "-u", "postgres"], text=True
        ).strip()
        gid = subprocess.check_output(
            ["docker", "exec", args.container, "id", "-g", "postgres"], text=True
        ).strip()
        if not uid.isdigit() or not gid.isdigit():
            raise ValueError("Invalid postgres user identity")
        created_id = subprocess.check_output(
            [
                "docker",
                "create",
                "--name",
                name,
                "--label",
                "vey.restore-drill=" + run_id,
                "--network",
                "none",
                "--read-only",
                "--user",
                uid + ":" + gid,
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--memory",
                "768m",
                "--cpus",
                "0.75",
                "--pids-limit",
                "128",
                "--tmpfs",
                f"/var/lib/postgresql/data:size=512m,uid={uid},gid={gid},mode=0700",
                "--tmpfs",
                f"/var/run/postgresql:size=4m,uid={uid},gid={gid},mode=0700",
                "--tmpfs",
                "/tmp:size=16m",
                "-e",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "-e",
                "POSTGRES_USER=" + source_admin,
                "-e",
                "PGDATA=/var/lib/postgresql/data",
                source["Image"],
                "postgres",
                "-c",
                "shared_buffers=16MB",
                "-c",
                "max_connections=10",
            ],
            text=True,
        ).strip()
        subprocess.run(["docker", "start", created_id], capture_output=True, check=True)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    created_id,
                    "sh",
                    "-c",
                    'test "$(cat /proc/1/comm)" = postgres && pg_isready -U "$POSTGRES_USER"',
                ],
                capture_output=True,
            )
            if result.returncode == 0:
                break
            time.sleep(1)
        else:
            raise TimeoutError("Restore PostgreSQL did not become ready")
        info = json.loads(subprocess.check_output(["docker", "inspect", created_id]))[0]
        require_isolated(info, created_id, run_id)
        for role in roles:
            query(
                created_id,
                "postgres",
                f"CREATE ROLE {identifier(role['name'])} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE {'INHERIT' if role['inherit'] else 'NOINHERIT'};",
            )
            for setting in role["settings"] or []:
                key, value = setting.split("=", 1)
                if not (
                    (key == "default_transaction_read_only" and value in {"on", "off"})
                    or (key == "statement_timeout" and re.fullmatch(r"[0-9]+(?:ms|s|min)?", value))
                ):
                    raise ValueError("Unreviewed role setting requires recovery review")
                query(
                    created_id,
                    "postgres",
                    f"ALTER ROLE {identifier(role['name'])} SET {identifier(key)} TO '{value}';",
                )
        for database, expected in expectations.items():
            report["stage"] = "restore_" + database
            security = expected["security"]
            if security["owner"] not in {*ROLES, source_admin}:
                raise ValueError("Unexpected database owner")
            query(
                created_id,
                "postgres",
                f"CREATE DATABASE {identifier(database)} OWNER {identifier(security['owner'])};",
            )
            with (destination / (database + ".dump")).open("rb") as input_file:
                subprocess.run(
                    [
                        "docker",
                        "exec",
                        "-i",
                        created_id,
                        "pg_restore",
                        "-U",
                        source_admin,
                        "-d",
                        database,
                        "--exit-on-error",
                    ],
                    stdin=input_file,
                    capture_output=True,
                    check=True,
                    timeout=120,
                )
            query(
                created_id, database, f"REVOKE ALL ON DATABASE {identifier(database)} FROM PUBLIC;"
            )
            for acl in security["acl"] or []:
                role = acl["role"]
                if role not in {*ROLES, source_admin, "PUBLIC"} or acl["privilege"] not in {
                    "CONNECT",
                    "CREATE",
                    "TEMPORARY",
                }:
                    raise ValueError("Unexpected database privilege")
                grantee = "PUBLIC" if role == "PUBLIC" else identifier(role)
                query(
                    created_id,
                    database,
                    f"GRANT {acl['privilege']} ON DATABASE {identifier(database)} TO {grantee}"
                    + (" WITH GRANT OPTION" if acl["grantable"] else "")
                    + ";",
                )
            actual = fingerprint(created_id, database)
            expected["fingerprint"]["views"] = canonical_views(
                created_id, database, expected["fingerprint"]["views"]
            )
            actual["views"] = canonical_views(created_id, database, actual["views"])
            if actual != expected["fingerprint"]:
                write_json(
                    destination / "restore-difference.json",
                    {"expected": expected["fingerprint"], "actual": actual},
                )
                report["mismatched_components"] = [
                    key for key in actual if actual[key] != expected["fingerprint"][key]
                ]
                raise RuntimeError("Restored data, views, ownership or privileges differ")
            # JSON ACL rows may order differently because role OIDs differ; compare semantic tuples.
            restored_security = database_security(created_id, database)

            def key(a):
                return a["role"], a["privilege"], a["grantable"]

            if security["owner"] != restored_security["owner"] or sorted(
                map(key, security["acl"])
            ) != sorted(map(key, restored_security["acl"])):
                raise RuntimeError("Restored database authorization differs")
            report["databases"][database]["data_views_permissions_verified"] = True
        query(
            created_id,
            "vey",
            "SET ROLE vey_dashboard; SELECT id FROM vey_dashboard.tasks LIMIT 1; SELECT id FROM vey_dashboard.events LIMIT 1; RESET ROLE;",
        )
        query(
            created_id,
            "vey_eval",
            "SET ROLE vey_eval_reader; SELECT run_id FROM vey_eval.runs LIMIT 1; RESET ROLE;",
        )
        # Validate the only runtime serial sequence without comparing its non-MVCC source state.
        sequence = query(
            created_id,
            "vey",
            "SELECT last_value >= coalesce((SELECT max(id) FROM vey_core.events),0) FROM vey_core.events_id_seq;",
        )
        if sequence != "t":
            raise RuntimeError("Restored event sequence is behind table rows")
        report["stage"] = "quarantine"
        report["quarantine"] = quarantine(created_id)
        report["role_passwords_restored"] = False
        report["roles_tested_without_login"] = len(roles)
        report["status"] = "passed"
        report["stage"] = "complete"
    except Exception as error:
        report["status"] = "failed"
        report["error_type"] = type(error).__name__
        raise
    finally:
        if created_id:
            try:
                info = json.loads(
                    subprocess.check_output(
                        ["docker", "inspect", created_id], stderr=subprocess.PIPE
                    )
                )[0]
                require_isolated(info, created_id, run_id)
            except (ValueError, KeyError, subprocess.SubprocessError):
                report["status"] = "failed"
                report["cleanup"] = "identity_mismatch"
            else:
                result = subprocess.run(
                    ["docker", "rm", "-f", "-v", created_id], capture_output=True
                )
                report["cleanup"] = "removed" if result.returncode == 0 else "failed"
                if result.returncode:
                    report["status"] = "failed"
        report["finished_at"] = utc()
        report["filesystem_after"] = dict(
            zip(("total", "used", "free"), shutil.disk_usage(backup_root), strict=True)
        )
        publish()
        if report["status"] == "passed":
            write_json(report_dir / "last-success.json", report, 0o640)
            if os.geteuid() == 0:
                os.chown(report_dir / "last-success.json", 0, 10000)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run(args)
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(0 if result["status"] == "passed" else 1)
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
