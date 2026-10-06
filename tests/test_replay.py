import hashlib
import json
import socket

import pytest

from vey.replay import MAX_BYTES, export_snapshot, load_snapshot, main, render_html


def detail():
    return {
        "task": {
            "id": "a" * 32,
            "result": "确认 ABCD1234 password=secret-value 124.223.196.18 user@example.com 13812345678",
            "user_id": "private-owner",
            "text": "original conversation",
        },
        "events": [
            {
                "id": 1,
                "kind": "operation_prepared",
                "created_at": "2026-10-06T00:00:00Z",
                "data": {"code": "ABCD1234", "action": "restart", "target": "demo/web"},
            },
            {
                "id": 2,
                "kind": "tool",
                "data": {"log_excerpt": "<script>alert(1)</script>", "cursor": "private-cursor"},
            },
        ],
        "events_truncated": True,
        "operation": {
            "action": "restart",
            "status": "pending",
            "container_ids": ["private-container"],
        },
    }


def wrap(payload):
    data = json.dumps(payload, ensure_ascii=False)
    return json.dumps(
        {
            "format": "vey-audit-v1",
            "payload": data,
            "sha256": hashlib.sha256(data.encode()).hexdigest(),
        }
    ).encode()


def test_export_roundtrip_redaction_and_non_executing_operation_records():
    raw = export_snapshot(detail())
    for secret in (
        "ABCD1234",
        "secret-value",
        "124.223.196.18",
        "user@example.com",
        "13812345678",
        "private-owner",
        "original conversation",
        "private-cursor",
        "private-container",
    ):
        assert secret.encode() not in raw
    snapshot = load_snapshot(raw)
    assert snapshot["events_truncated"] is True
    assert snapshot["operation"]["action"] == "restart"
    assert len(snapshot["events"]) == 2
    rendered = render_html(snapshot)
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "default-src 'none'" in rendered


def test_snapshot_rejects_tampering_invalid_order_and_size():
    raw = export_snapshot(detail())
    with pytest.raises(ValueError, match="checksum"):
        load_snapshot(raw.replace(b"demo/web", b"demo/api"))
    payload = load_snapshot(raw)
    payload["events"].reverse()
    with pytest.raises(ValueError, match="out-of-order"):
        load_snapshot(wrap(payload))
    payload["events"] = [{"id": 1, "kind": "shell", "data": {"cmd": "rm"}}]
    with pytest.raises(ValueError):
        load_snapshot(wrap(payload))
    with pytest.raises(ValueError):
        load_snapshot(b"x" * (MAX_BYTES + 1))
    huge = detail()
    huge["task"]["result"] = "x" * MAX_BYTES
    with pytest.raises(ValueError):
        export_snapshot(huge)


def test_cli_offline_review_has_no_network_and_never_overwrites(tmp_path, monkeypatch, capsys):
    def forbidden(*a, **kw):
        raise AssertionError("Network is forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)
    source = tmp_path / "snapshot.json"
    target = tmp_path / "review.html"
    source.write_bytes(export_snapshot(detail()))
    assert main([str(source), "--html", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["executed_actions"] == 0
    before = target.read_bytes()
    with pytest.raises(SystemExit):
        main([str(source), "--html", str(target)])
    assert target.read_bytes() == before
