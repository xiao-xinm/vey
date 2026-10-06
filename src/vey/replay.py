"""Validate and step through an exported audit snapshot without network or tool execution."""

import argparse
import hashlib
import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from vey.security import safe_value

MAX_BYTES = 1_000_000
FORMAT = "vey-audit-v1"
KINDS = {
    "intent",
    "intent_guard",
    "tool",
    "model",
    "diagnosis",
    "operation_prepared",
    "operation",
    "error",
}
WARNINGS = [
    "这是有限审计摘要，不能恢复完整日志、原始对话或模型内部思维链。",
    "离线复核只按顺序展示记录，不调用模型、Docker 或网络，不重新确认或执行操作。",
    "SHA-256 只检查文件内容一致性，不证明导出者身份，也不证明诊断正确。",
    "已做规则脱敏，未知格式的敏感信息仍须人工检查后再分享。",
]


def export_clean(value):
    if isinstance(value, dict):
        return {
            k: "[REDACTED]"
            if re.fullmatch(
                r"(?i)(?:.*_)?(?:password|passwd|pwd|secret|token|api_?key|authorization|cookie)(?:_.*)?",
                k,
            )
            else export_clean(v)
            for k, v in value.items()
            if k.lower()
            not in {
                "user_id",
                "message_id",
                "generation",
                "cursor",
                "text",
                "history",
                "container_ids",
                "policy_hash",
                "authorization",
                "cookie",
            }
        }
    if isinstance(value, list):
        return [export_clean(v) for v in value]
    if isinstance(value, str):
        if len(value) > 12000:
            raise ValueError("Single audit field exceeds 12000 characters")
        value = safe_value(value)
        value = re.sub(r"\b[A-Fa-f0-9]{8}\b", "[隐藏确认码]", value)
        value = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[IP]", value)
        value = re.sub(r"[\w.+-]{1,64}@[\w.-]{1,253}\.[a-zA-Z]{2,63}", "[EMAIL]", value)
        value = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[PHONE]", value)
    return value


def export_snapshot(detail):
    if len(json.dumps(detail, ensure_ascii=False, allow_nan=False).encode()) > MAX_BYTES:
        raise ValueError("Export exceeds 1 MB; choose a smaller task")
    # An inner JSON string has an unambiguous cross-language byte representation for hashing.
    payload = export_clean(
        {
            "format": FORMAT,
            "exported_at": datetime.now(UTC).isoformat(),
            "task": detail["task"],
            "events": detail["events"],
            "events_truncated": detail["events_truncated"],
            "operation": detail["operation"],
            "warnings": WARNINGS,
        }
    )
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    envelope = {
        "format": FORMAT,
        "payload": data,
        "sha256": hashlib.sha256(data.encode()).hexdigest(),
    }
    encoded = json.dumps(envelope, ensure_ascii=False, indent=2).encode()
    if len(encoded) > MAX_BYTES:
        raise ValueError("Export exceeds 1 MB; choose a smaller task")
    return encoded


def load_snapshot(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError("Snapshot exceeds 1 MB")
    try:
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or set(envelope) != {"format", "payload", "sha256"}:
            raise ValueError("Invalid envelope")
        if envelope["format"] != FORMAT or not isinstance(envelope["payload"], str):
            raise ValueError("Unsupported snapshot format")
        if hashlib.sha256(envelope["payload"].encode()).hexdigest() != envelope["sha256"]:
            raise ValueError("Snapshot checksum mismatch")
        payload = json.loads(envelope["payload"])
        if not isinstance(payload, dict) or payload.get("format") != FORMAT:
            raise ValueError("Invalid snapshot")
        if not isinstance(payload.get("task"), dict) or not re.fullmatch(
            r"[a-f0-9]{32}", payload["task"].get("id", "")
        ):
            raise ValueError("Invalid task")
        events = payload.get("events")
        if not isinstance(events, list) or len(events) > 100:
            raise ValueError("At most 100 audit events are supported")
        last = -1
        for e in events:
            if (
                not isinstance(e, dict)
                or type(e.get("id")) is not int
                or e["id"] <= last
                or e.get("kind") not in KINDS
                or not isinstance(e.get("data"), dict)
            ):
                raise ValueError("Invalid or out-of-order audit event")
            last = e["id"]
        if type(payload.get("events_truncated")) is not bool:
            raise ValueError("Missing truncation declaration")
        return payload
    except (KeyError, TypeError, RecursionError, UnicodeError) as error:
        raise ValueError("Invalid snapshot structure") from error


def render_html(payload):
    # No script, external URL, form, model or tool entry point. Each event is an inert record.
    body = [
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>",
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; base-uri 'none'; form-action 'none'\">",
        "<title>Vey 离线审计复核</title><h1>Vey 离线审计复核</h1>",
    ]
    body += ["<p>" + html.escape(w) + "</p>" for w in WARNINGS]
    if payload["events_truncated"]:
        body.append("<p>导出已截断，后续事件不在文件中。</p>")
    for e in payload["events"]:
        body.append(
            "<details><summary>"
            + html.escape(f"记录 {e['id']} · {e['kind']}")
            + "</summary><pre>"
            + html.escape(json.dumps(e, ensure_ascii=False, indent=2))
            + "</pre></details>"
        )
    body.append(
        "<h2>任务摘要</h2><pre>"
        + html.escape(json.dumps(payload["task"], ensure_ascii=False, indent=2))
        + "</pre></html>"
    )
    return "\n".join(body)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Offline audit replay; no model, network or Docker calls"
    )
    parser.add_argument("snapshot", type=Path)
    parser.add_argument(
        "--html", type=Path, help="Create a new standalone, script-free HTML review"
    )
    args = parser.parse_args(argv)
    try:
        with args.snapshot.open("rb") as f:
            payload = load_snapshot(f.read(MAX_BYTES + 1))
        if args.html:
            with args.html.open("x", encoding="utf-8") as f:
                f.write(render_html(payload))
        print(
            json.dumps(
                {
                    "format": FORMAT,
                    "task_id": payload["task"]["id"],
                    "events": len(payload["events"]),
                    "truncated": payload["events_truncated"],
                    "executed_actions": 0,
                    "warnings": WARNINGS,
                },
                ensure_ascii=False,
            )
        )
        return 0
    except (OSError, ValueError, RecursionError) as error:
        parser.error(
            f"Cannot review snapshot ({type(error).__name__}); check format, checksum and size"
        )


if __name__ == "__main__":
    raise SystemExit(main())
