"""Dashboard queries use a separate SELECT-only identity, never the archive writer."""

from psycopg import sql
from psycopg.rows import dict_row

from vey.domain import VeyError
from vey.evaluation.archive import connect_archive
from vey.security import safe_value


def install_reader(db, password, role="vey_eval_reader"):
    name = db.execute("SELECT current_database()").fetchone()[0]
    if name != "vey_eval" and not name.startswith("vey_test_eval"):
        raise ValueError("Dedicated evaluation database required")
    if (
        len(password) < 32
        or db.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
    ):
        raise ValueError("Use a new independent role and 32+ character password")
    db.execute(
        sql.SQL(
            "CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}"
        ).format(sql.Identifier(role), sql.Literal(password))
    )
    db.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(name), sql.Identifier(role)
        )
    )
    db.execute(sql.SQL("GRANT USAGE ON SCHEMA vey_eval TO {}").format(sql.Identifier(role)))
    db.execute(
        sql.SQL("GRANT SELECT ON vey_eval.runs,vey_eval.samples TO {}").format(sql.Identifier(role))
    )
    db.execute(
        sql.SQL("ALTER ROLE {} SET default_transaction_read_only=on").format(sql.Identifier(role))
    )
    db.execute(sql.SQL("ALTER ROLE {} SET statement_timeout='5s'").format(sql.Identifier(role)))


# Explicit projection avoids reading the full (up to 10 MB) report for a list entry.
RUN_COLUMNS = """run_id,scope,dataset_hash,report_hash,archived_at,
    report->>'dataset_version' AS dataset_version, report->>'implementation_hash' AS implementation_hash,
    report->>'mode' AS mode, report->>'status' AS status, report->>'split' AS split,
    report->>'strategy' AS strategy, report->>'model' AS model,
    report->>'prompt_version' AS prompt_version, report->>'loop_version' AS loop_version,
    report->>'scoring_version' AS scoring_version, report->>'started_at' AS started_at,
    report->>'finished_at' AS finished_at, report->'summary' AS summary,
    report->'executed' AS executed, report->'scheduled' AS scheduled,
    report->'not_run' AS not_run, report->'passed' AS passed,
    report->'model_calls' AS model_calls, report->'tokens' AS tokens,
    report->'estimated_cost' AS estimated_cost, report->'pricing' AS pricing"""


def normalize_run(row):
    row = dict(row)
    row["archived_at"] = row["archived_at"].isoformat()
    summary = row.pop("summary")
    row["metrics"] = (
        summary
        if summary is not None
        else {
            key: row[key]
            for key in (
                "executed",
                "scheduled",
                "not_run",
                "passed",
                "model_calls",
                "tokens",
                "estimated_cost",
            )
        }
    )
    row["scoring_label"] = row["scoring_version"] or "历史报告未记录评分版本"
    row["provenance_note"] = (
        "公开版本化案例；已分析过的数据属于已知回归。通过率是契约评分，不代表生产准确率。"
    )
    return safe_value(row)


class EvaluationReader:
    def __init__(self, url):
        self.url = url

    def runs(self, before=None, limit=20):
        with connect_archive(self.url) as db, db.cursor(row_factory=dict_row) as cursor:
            params, where = {"limit": limit + 1}, ""
            if before:
                anchor = cursor.execute(
                    "SELECT archived_at,run_id FROM vey_eval.runs WHERE run_id=%s", (before,)
                ).fetchone()
                if anchor is None:
                    raise VeyError("expired_cursor", "评测分页位置不存在，请刷新", 409)
                params.update(anchor)
                where = " WHERE (archived_at,run_id)<(%(archived_at)s,%(run_id)s)"
            rows = cursor.execute(
                "SELECT "
                + RUN_COLUMNS
                + " FROM vey_eval.runs"
                + where
                + " ORDER BY archived_at DESC,run_id DESC LIMIT %(limit)s",
                params,
            ).fetchall()
        return {
            "items": [normalize_run(row) for row in rows[:limit]],
            "next": rows[limit - 1]["run_id"] if len(rows) > limit else None,
        }

    def detail(self, run_id, offset=0, status=None, limit=10):
        with connect_archive(self.url) as db, db.cursor(row_factory=dict_row) as cursor:
            row = cursor.execute(
                "SELECT " + RUN_COLUMNS + " FROM vey_eval.runs WHERE run_id=%s", (run_id,)
            ).fetchone()
            if row is None:
                raise VeyError("not_found", "评测运行不存在", 404)
            where, params = "run_id=%(id)s", {"id": run_id, "offset": offset, "limit": limit + 1}
            if status:
                where += " AND status=%(status)s"
                params["status"] = status
            samples = cursor.execute(
                """SELECT case_id,repetition,status,
                CASE WHEN octet_length(result::text)<=200000 THEN result
                     ELSE jsonb_build_object('summary','样本超过 200 KB，未加载正文') END AS result,
                octet_length(result::text)>200000 AS truncated
                FROM vey_eval.samples WHERE """
                + where
                + " ORDER BY case_id,repetition LIMIT %(limit)s OFFSET %(offset)s",
                params,
            ).fetchall()
            versions = cursor.execute(
                """SELECT DISTINCT result->>'prompt_version' AS prompt_version
                FROM vey_eval.samples WHERE run_id=%s AND result ? 'prompt_version' LIMIT 20""",
                (run_id,),
            ).fetchall()
        return {
            "run": normalize_run(row),
            "samples": safe_value(samples[:limit]),
            "prompt_versions": [v["prompt_version"] for v in versions],
            "next_offset": offset + limit if len(samples) > limit else None,
        }
