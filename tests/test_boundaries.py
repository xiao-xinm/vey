import base64
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from vey.config import Settings
from vey.docker_backend import DockerBackend
from vey.domain import ToolCall, VeyError
from vey.logs import page_logs, sanitize_records
from vey.model import DeepSeekProvider, deterministic_intent
from vey.security import safe_value
from vey.wecom import WeComCipher, chunks_utf8, parse_xml


def test_policy_requires_dependency_protection(policy):
    with pytest.raises(ValidationError):
        type(policy).model_validate(
            {
                **policy.model_dump(),
                "protected_projects": [],
                "database_protection_acknowledged": False,
            }
        )


@pytest.mark.parametrize("name", ["shell", "exec", "remove", "prune", "create"])
def test_read_tools_cannot_become_shell(name):
    with pytest.raises(ValidationError):
        ToolCall(name=name, target="blog/web")


def test_logs_require_bounds_and_timezone():
    with pytest.raises(ValidationError):
        ToolCall(name="logs", target="blog/web", lines=501)
    with pytest.raises(ValidationError):
        ToolCall(name="logs", target="blog/web", lines="100")
    with pytest.raises(ValidationError):
        ToolCall(name="logs", target="blog/web", since="2026-09-25T12:00:00")
    with pytest.raises(ValidationError):
        ToolCall(name="inspect", target="blog/web", command="whoami")


def test_log_pages_do_not_repeat_or_skip():
    original = [str(i) for i in range(301)]
    page1 = page_logs(original)
    page2 = page_logs(page1.remaining)
    page3 = page_logs(page2.remaining)
    page4 = page_logs(page3.remaining)
    assert page1.text.splitlines() == original[-100:]
    assert (
        page4.text.splitlines()
        + page3.text.splitlines()
        + page2.text.splitlines()
        + page1.text.splitlines()
        == original
    )


def test_long_line_continues_before_older_lines():
    first = page_logs(["older", "中" * 25001], requested=1)
    second = page_logs(first.remaining, 1, pending=first.pending)
    third = page_logs(second.remaining, 1, pending=second.pending)
    assert len(first.text) == len(second.text) == 10000
    assert first.text + second.text + third.text == "中" * 25001
    assert third.remaining == ["older"]
    assert first.truncated and first.reason == "characters"


def test_log_hard_line_cap_reports_truncation():
    page = page_logs([str(i) for i in range(600)], requested=500)
    assert len(page.text.splitlines()) == 500
    assert page.truncated and page.reason == "lines" and len(page.remaining) == 100


def test_default_log_snapshot_does_not_drop_current_second(settings):
    captured = []

    class Container:
        def logs(self, **kwargs):
            captured.append(kwargs)
            return iter([b"2026-09-26T01:33:00.123456789Z fresh-log\n"])

    class Containers:
        def get(self, _):
            return Container()

    class Client:
        containers = Containers()

    backend = DockerBackend.__new__(DockerBackend)
    backend.settings, backend.client = settings, Client()
    records, _ = backend.logs(["fixture"], ToolCall(name="logs", target="fixture/web"))
    assert records[0].endswith("fresh-log")
    assert captured[0]["until"] is None and captured[0]["follow"] is False
    explicit = ToolCall(name="logs", target="fixture/web", until="2026-09-26T01:33:00Z")
    backend.logs(["fixture"], explicit)
    assert captured[1]["until"] == explicit.until


def test_container_summary_keeps_timestamps_without_environment_secrets():
    container = SimpleNamespace(
        id="fixture",
        name="fixture-web",
        status="running",
        attrs={
            "State": {
                "Status": "running",
                "StartedAt": "2026-09-26T01:00:00Z",
                "FinishedAt": "0001-01-01T00:00:00Z",
            },
            "Config": {
                "Env": ["PASSWORD=do-not-export"],
                "Image": "nginx",
                "Labels": {"com.docker.compose.project": "fixture"},
            },
        },
    )
    result = DockerBackend.__new__(DockerBackend)._summary(container)
    assert result["started_at"] == "2026-09-26T01:00:00Z"
    assert result["finished_at"] == "0001-01-01T00:00:00Z"
    assert "do-not-export" not in json.dumps(result) and "Env" not in result


def test_secret_file_setting_works_from_dotenv(tmp_path):
    token = tmp_path / "token"
    token.write_text("s" * 40, encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text(
        f"VEY_DATABASE_URL=postgresql+psycopg://test@localhost/vey_test\nVEY_EXECUTOR_TOKEN_FILE={token.as_posix()}\n",
        encoding="utf-8",
    )
    config = Settings(_env_file=env)
    assert config.executor_token.get_secret_value() == "s" * 40


def test_multiline_and_common_secrets_are_redacted():
    original = 'password="hello world" token=abc postgres://bob:pass@db/x\n-----BEGIN PRIVATE KEY-----\nsecret material\n-----END PRIVATE KEY-----'
    output = "\n".join(sanitize_records(original.splitlines()))
    for secret in ["hello world", "abc", "bob:pass", "secret material"]:
        assert secret not in output
    assert safe_value({"prompt_tokens": 5, "access_token": "secret"}) == {
        "prompt_tokens": 5,
        "access_token": "[REDACTED]",
    }


def test_negation_never_uses_simple_operation_parser(policy):
    assert deterministic_intent("先别重启，看看博客日志", policy, {}) is None
    assert deterministic_intent("重启 博客", policy, {}).action == "restart"


def test_wecom_cipher_verifies_signature_corp_padding_and_time():
    key = base64.b64encode(bytes(range(32))).decode().rstrip("=")
    cipher = WeComCipher("token", key, "corp")
    timestamp, nonce = str(int(time.time())), "nonce"
    encrypted = cipher.encrypt("测试消息".encode())
    signature = cipher.signature(timestamp, nonce, encrypted)
    assert cipher.decrypt(encrypted, signature, timestamp, nonce).decode() == "测试消息"
    for bad_signature, bad_time in [("0" * 40, timestamp), (signature, "1")]:
        with pytest.raises(VeyError):
            cipher.decrypt(encrypted, bad_signature, bad_time, nonce)
    with pytest.raises(VeyError):
        WeComCipher("token", key, "other").decrypt(encrypted, signature, timestamp, nonce)


def test_xml_entities_and_oversized_payload_rejected():
    with pytest.raises(VeyError):
        parse_xml(b'<!DOCTYPE xml [<!ENTITY a SYSTEM "file:///secret">]><xml>&a;</xml>')
    with pytest.raises(VeyError):
        parse_xml(b"x" * 65537)


def test_wecom_utf8_chunks_preserve_text():
    original = "中文🙂" * 1000
    chunks = chunks_utf8(original)
    assert "".join(chunks) == original
    assert all(len(chunk.encode()) <= 1800 for chunk in chunks)


async def test_deepseek_output_is_validated_and_reasoning_not_stored(settings):
    def handler(request):
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert body["thinking"]["type"] == "disabled"
        assert "my-password" not in str(body)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"kind":"query","tool":{"name":"system"}}',
                            "reasoning_content": "private reasoning",
                        },
                    }
                ],
                "usage": {"prompt_tokens": 7},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DeepSeekProvider(settings, client)
    intent = await provider.route("password=my-password 服务器情况", {}, [])
    assert intent.tool.name == "system"
    assert provider.metrics[0]["usage"]["prompt_tokens"] == 7
    assert "private reasoning" not in str(provider.metrics)
    await provider.close()


async def test_model_truncation_cannot_trigger_operation(settings):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "choices": [
                        {"finish_reason": "length", "message": {"content": '{"kind":"operation"}'}}
                    ]
                },
            )
        )
    )
    provider = DeepSeekProvider(settings, client)
    with pytest.raises(VeyError, match="格式无效"):
        await provider.route("查询", {}, [])
    await provider.close()


async def test_model_stats_without_target_is_rejected(settings):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": '{"kind":"query","tool":{"name":"stats"}}'},
                        }
                    ]
                },
            )
        )
    )
    provider = DeepSeekProvider(settings, client)
    with pytest.raises(VeyError, match="格式无效"):
        await provider.route("这台服务器的 CPU 和内存使用情况", {}, [])
    assert provider.metrics[0]["status"] == "error"
    await provider.close()
