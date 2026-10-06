"""Create only the dashboard views and a new independent SELECT-only role."""

import getpass
import os

import psycopg

from vey.dashboard_db import install_views

if __name__ == "__main__":
    password = getpass.getpass("New independent dashboard database password (32+ chars): ")
    url = os.environ["VEY_MIGRATION_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as db:
        install_views(db, password)
    print("Dashboard read-only views and independent database role created.")
