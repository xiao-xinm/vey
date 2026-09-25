"""Initial immutable schema snapshot. Migration is a manual deployment step."""

from alembic import op

revision = "0001"
down_revision = None


def upgrade():
    op.execute("CREATE SCHEMA IF NOT EXISTS vey_core")
    op.execute("CREATE SCHEMA IF NOT EXISTS vey_exec")
    op.execute(
        "CREATE TABLE vey_core.sessions (\n\tuser_id VARCHAR(128) NOT NULL, \n\tgeneration VARCHAR(64) NOT NULL, \n\tstarted_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcontext JSONB NOT NULL, \n\tPRIMARY KEY (user_id)\n)"
    )
    op.execute(
        "CREATE TABLE vey_core.tasks (\n\tid VARCHAR(32) NOT NULL, \n\tmessage_id VARCHAR(160) NOT NULL, \n\tuser_id VARCHAR(128) NOT NULL, \n\tgeneration VARCHAR(64) NOT NULL, \n\ttext TEXT NOT NULL, \n\tchannel VARCHAR(16) NOT NULL, \n\tstatus VARCHAR(24) NOT NULL, \n\tresult TEXT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (id), \n\tUNIQUE (message_id)\n)"
    )
    op.execute("CREATE INDEX ix_tasks_queue ON vey_core.tasks (status, created_at)")
    op.execute(
        "CREATE TABLE vey_exec.grants (\n\tcode VARCHAR(8) NOT NULL, \n\ttask_id VARCHAR(32) NOT NULL, \n\tuser_id VARCHAR(128) NOT NULL, \n\tgeneration VARCHAR(64) NOT NULL, \n\ttarget VARCHAR(120) NOT NULL, \n\taction VARCHAR(16) NOT NULL, \n\tcontainer_ids JSONB NOT NULL, \n\tpolicy_hash VARCHAR(64) NOT NULL, \n\tstatus VARCHAR(24) NOT NULL, \n\tresult JSONB NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (code), \n\tUNIQUE (task_id)\n)"
    )
    op.execute("CREATE INDEX ix_vey_exec_grants_target ON vey_exec.grants (target)")
    op.execute(
        "CREATE TABLE vey_exec.log_cursors (\n\tid VARCHAR(32) NOT NULL, \n\tuser_id VARCHAR(128) NOT NULL, \n\tgeneration VARCHAR(64) NOT NULL, \n\tcontainer_ids JSONB NOT NULL, \n\ttarget VARCHAR(120) NOT NULL, \n\tpayload JSONB NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id)\n)"
    )
    op.execute("CREATE INDEX ix_vey_exec_log_cursors_user_id ON vey_exec.log_cursors (user_id)")
    op.execute(
        "CREATE TABLE vey_exec.sessions (\n\tuser_id VARCHAR(128) NOT NULL, \n\tgeneration VARCHAR(64) NOT NULL, \n\tstarted_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (user_id)\n)"
    )
    op.execute(
        "CREATE TABLE vey_core.events (\n\tid SERIAL NOT NULL, \n\ttask_id VARCHAR(32) NOT NULL, \n\tkind VARCHAR(32) NOT NULL, \n\tdata JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(task_id) REFERENCES vey_core.tasks (id) ON DELETE CASCADE\n)"
    )
    op.execute("CREATE INDEX ix_vey_core_events_task_id ON vey_core.events (task_id)")
    op.execute(
        "CREATE TABLE vey_core.outbox (\n\tid VARCHAR(32) NOT NULL, \n\ttask_id VARCHAR(32) NOT NULL, \n\tkind VARCHAR(16) NOT NULL, \n\tuser_id VARCHAR(128) NOT NULL, \n\tbody TEXT NOT NULL, \n\tstatus VARCHAR(24) NOT NULL, \n\tattempts INTEGER NOT NULL, \n\tnext_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (task_id, kind), \n\tFOREIGN KEY(task_id) REFERENCES vey_core.tasks (id) ON DELETE CASCADE\n)"
    )


def downgrade():
    raise RuntimeError("Restore a verified backup instead of dropping production data.")
