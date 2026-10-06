"""Read-only projection, installed by an administrator; no runtime schema changes."""

from psycopg import sql


def install_views(db, password: str, role: str = "vey_dashboard"):
    name = db.execute("SELECT current_database()").fetchone()[0]
    if name != "vey" and not name.startswith("vey_test"):
        raise ValueError("Dedicated Vey database required")
    if len(password) < 32:
        raise ValueError("Use a random password of at least 32 characters")
    if db.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
        raise ValueError("Dashboard role already exists; inspect it before changing permissions")
    db.execute(
        sql.SQL(
            "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD {}"
        ).format(sql.Identifier(role), sql.Literal(password))
    )
    db.execute("CREATE SCHEMA vey_dashboard")
    db.execute("REVOKE ALL ON SCHEMA vey_dashboard FROM PUBLIC")
    db.execute("""
        CREATE VIEW vey_dashboard.tasks WITH (security_barrier=true) AS
        SELECT id, status, channel, created_at, updated_at,
          regexp_replace(result, '\\m[A-Fa-f0-9]{8}\\M', '[隐藏确认码]', 'g') AS result,
          (SELECT data - 'message' FROM vey_core.events e
           WHERE e.task_id=t.id AND e.kind='intent' ORDER BY e.id LIMIT 1) AS intent
        FROM vey_core.tasks t
    """)
    db.execute("""
        CREATE VIEW vey_dashboard.events WITH (security_barrier=true) AS
        SELECT id, task_id, kind, created_at,
          CASE WHEN kind='operation_prepared' THEN data - 'code' ELSE data END AS data
        FROM vey_core.events
        WHERE kind IN ('intent','intent_guard','tool','model','diagnosis',
                       'operation_prepared','operation','error')
    """)
    db.execute("""
        CREATE VIEW vey_dashboard.operations WITH (security_barrier=true) AS
        SELECT task_id, target, action, status, created_at, updated_at, expires_at, policy_hash
        FROM vey_exec.grants
    """)
    db.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(name), sql.Identifier(role)
        )
    )
    db.execute(sql.SQL("GRANT USAGE ON SCHEMA vey_dashboard TO {}").format(sql.Identifier(role)))
    db.execute(
        sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA vey_dashboard TO {}").format(
            sql.Identifier(role)
        )
    )
    db.execute(
        sql.SQL("ALTER ROLE {} SET default_transaction_read_only=on").format(sql.Identifier(role))
    )
    db.execute(sql.SQL("ALTER ROLE {} SET statement_timeout='5s'").format(sql.Identifier(role)))
