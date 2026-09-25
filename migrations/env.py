import os

from alembic import context
from sqlalchemy import create_engine

from vey.db import Base

url = os.environ["VEY_MIGRATION_DATABASE_URL"]
engine = create_engine(url)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata, include_schemas=True)
    with context.begin_transaction():
        context.run_migrations()
