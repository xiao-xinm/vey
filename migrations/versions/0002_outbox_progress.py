"""Persist acknowledged message parts so retries resume instead of replaying a result."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade():
    op.add_column(
        "outbox",
        sa.Column("sent_parts", sa.Integer(), nullable=False, server_default="0"),
        schema="vey_core",
    )


def downgrade():
    raise RuntimeError("Restore a verified backup instead of discarding delivery progress.")
