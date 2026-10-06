"""Administrator-only initialization of a separate archive SELECT-only role."""

import argparse
import getpass
from pathlib import Path

from vey.evaluation.archive import connect_archive
from vey.evaluation.reader import install_reader

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin-url-file", type=Path, required=True)
    args = parser.parse_args()
    password = getpass.getpass("New evaluation reader database password (32+ random chars): ")
    with connect_archive(args.admin_url_file.read_text().strip()) as db:
        install_reader(db, password)
    print("Independent SELECT-only evaluation reader created.")
