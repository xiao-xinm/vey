"""Versioned service configuration; runtime roles cannot rewrite version history."""

from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade():
    op.execute("""CREATE TABLE vey_exec.policy_versions (
        id varchar(32) PRIMARY KEY, parent_id varchar(32), services jsonb NOT NULL,
        reason varchar(300) NOT NULL, rollback_of varchar(32), created_at timestamptz NOT NULL)""")
    op.execute("""CREATE TABLE vey_exec.policy_state (
        id integer PRIMARY KEY CHECK (id=1), active_id varchar(32) NOT NULL
        REFERENCES vey_exec.policy_versions(id), baseline_hash varchar(64) NOT NULL)""")
    op.execute("""CREATE FUNCTION vey_exec.reject_policy_rewrite() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Policy history is append-only'; END $$""")
    op.execute("""CREATE TRIGGER policy_history_immutable BEFORE UPDATE OR DELETE
        ON vey_exec.policy_versions FOR EACH ROW EXECUTE FUNCTION vey_exec.reject_policy_rewrite()""")
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='vey_executor') THEN
        GRANT SELECT,INSERT ON vey_exec.policy_versions TO vey_executor;
        REVOKE UPDATE,DELETE,TRUNCATE ON vey_exec.policy_versions FROM vey_executor;
        GRANT SELECT,INSERT,UPDATE ON vey_exec.policy_state TO vey_executor;
        REVOKE DELETE,TRUNCATE ON vey_exec.policy_state FROM vey_executor;
        END IF; END $$""")


def downgrade():
    raise RuntimeError("Configuration history must not be dropped during rollback")
