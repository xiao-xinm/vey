"""Initialize only a dedicated Vey database. Run manually with a migration owner.

Never run against a business database. Passwords are read from terminal without echo.
"""

import getpass
import os
import subprocess
import sys

import psycopg
from psycopg import sql


def grant_runtime_access(db, passwords):
    name = db.execute("SELECT current_database()").fetchone()[0]
    if name != "vey" and not name.startswith("vey_test"):
        raise ValueError("Dedicated Vey database required")
    db.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name)))
    db.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    for role, password in passwords.items():
        db.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
    for role, schema in (("vey_core", "vey_core"), ("vey_executor", "vey_exec")):
        db.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema)))
        db.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
        db.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
        db.execute(
            sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
        db.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
            ).format(sql.Identifier(schema), sql.Identifier(role))
        )
        db.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT USAGE, SELECT ON SEQUENCES TO {}"
            ).format(sql.Identifier(schema), sql.Identifier(role))
        )
    for role in passwords:
        db.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(name), sql.Identifier(role)
            )
        )


def main():
    url = os.environ["VEY_MIGRATION_DATABASE_URL"]
    with psycopg.connect(
        url.replace("postgresql+psycopg://", "postgresql://"), autocommit=True
    ) as db:
        name = db.execute("SELECT current_database()").fetchone()[0]
        if name != "vey" and not name.startswith("vey_test"):
            raise SystemExit("Refusing to initialize outside dedicated vey/vey_test* database")
        for role in ("vey_core", "vey_executor"):
            if db.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                raise SystemExit(f"Role {role} already exists; inspect ownership before reusing it")
        passwords = {
            role: getpass.getpass(f"New password for {role}: ")
            for role in ("vey_core", "vey_executor")
        }
        if any(len(value) < 20 for value in passwords.values()):
            raise SystemExit("Use independent passwords of at least 20 characters")
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
        with db.transaction():
            grant_runtime_access(db, passwords)
        print(
            "Vey schemas and separate runtime roles initialized. No existing business tables changed."
        )


if __name__ == "__main__":
    main()
