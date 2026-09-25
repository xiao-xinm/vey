import re
import secrets

from vey.domain import VeyError

SECRET_PAIR = re.compile(
    r"""(?i)(["']?(?:password|passwd|pwd|secret|token|api[_-]?key|authorization|cookie)["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}]+)"""
)
URL_AUTH = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@")
BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
PRIVATE_KEY = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S)


def redact(text: str) -> str:
    text = PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
    text = URL_AUTH.sub(r"\1[REDACTED]@", text)
    text = BEARER.sub("Bearer [REDACTED]", text)
    return SECRET_PAIR.sub(r"\1[REDACTED]", text)


def safe_value(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [safe_value(v) for v in value]
    if isinstance(value, dict):
        return {
            k: "[REDACTED]"
            if re.fullmatch(
                r"(?i)(?:.*_)?(?:password|secret|token|api_key|authorization|cookie)", k
            )
            else safe_value(v)
            for k, v in value.items()
        }
    return value


def verify_token(actual: str | None, expected: str):
    if not actual or not secrets.compare_digest(actual, "Bearer " + expected):
        raise VeyError("unauthorized", "未授权", 401)
