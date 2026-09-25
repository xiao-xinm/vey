"""Manual acknowledgement of an unknown result; never repeats Docker operations."""

import argparse
import os

from sqlalchemy import select, text

from vey.db import Grant, database
from vey.domain import utcnow
from vey.security import redact


def reconcile(factory, task_id, note):
    if len(note.strip()) < 10:
        raise ValueError("请记录核对时间、容器状态和解除锁定的理由，至少 10 字符")
    with factory.begin() as db:
        grant = db.scalar(select(Grant).where(Grant.task_id == task_id).with_for_update())
        if not grant or grant.status != "unknown":
            raise ValueError("仅可人工核对 unknown 操作；执行中的任务不能解除锁定")
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:target, 0))"),
            {"target": grant.target},
        )
        grant.result = {
            **grant.result,
            "reconciliation": {
                "previous_status": "unknown",
                "note": redact(note)[:2000],
                "at": utcnow().isoformat(),
                "operator": "local_database_admin",
            },
        }
        grant.status, grant.updated_at = "reconciled", utcnow()
        return {
            "task_id": task_id,
            "status": "reconciled",
            "note": "已记录人工核对并解除后续操作锁定，没有重试原操作，也不认定原操作成功",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id")
    parser.add_argument("--note", required=True)
    args = parser.parse_args()
    engine, factory = database(os.environ["VEY_MIGRATION_DATABASE_URL"])
    try:
        print(reconcile(factory, args.task_id, args.note))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
