from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import struct
import time

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from defusedxml import ElementTree

from vey.domain import VeyError
from vey.security import redact


class WeComCipher:
    def __init__(self, token: str, encoding_key: str, corp_id: str):
        self.token, self.corp_id = token, corp_id
        try:
            self.key = base64.b64decode(encoding_key + "=", validate=True)
        except ValueError:
            raise ValueError("企微 EncodingAESKey 无效") from None
        if len(self.key) != 32 or not token or not corp_id:
            raise ValueError("企微加密配置不完整")

    def signature(self, timestamp, nonce, encrypted):
        return hashlib.sha1(
            "".join(sorted([self.token, timestamp, nonce, encrypted])).encode()
        ).hexdigest()

    def decrypt(
        self, encrypted: str, signature: str, timestamp: str, nonce: str, *, check_time=True
    ) -> bytes:
        try:
            if check_time and abs(time.time() - int(timestamp)) > 300:
                raise ValueError()
            if not hmac.compare_digest(self.signature(timestamp, nonce, encrypted), signature):
                raise ValueError()
            ciphertext = base64.b64decode(encrypted, validate=True)
            decryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).decryptor()
            plain = decryptor.update(ciphertext) + decryptor.finalize()
            padding = plain[-1]
            if not 1 <= padding <= 32 or plain[-padding:] != bytes([padding]) * padding:
                raise ValueError()
            plain = plain[:-padding]
            size = struct.unpack("!I", plain[16:20])[0]
            if size > len(plain) - 20 or plain[20 + size :].decode() != self.corp_id:
                raise ValueError()
            return plain[20 : 20 + size]
        except (ValueError, IndexError, struct.error, UnicodeError):
            raise VeyError("invalid_callback", "回调验证失败", 403) from None

    def encrypt(self, content: bytes) -> str:
        plain = os.urandom(16) + struct.pack("!I", len(content)) + content + self.corp_id.encode()
        padding = 32 - len(plain) % 32
        plain += bytes([padding]) * padding
        encryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).encryptor()
        return base64.b64encode(encryptor.update(plain) + encryptor.finalize()).decode()


def parse_xml(body: bytes):
    if len(body) > 65536:
        raise VeyError("oversized_message", "消息过大", 413)
    try:
        return ElementTree.fromstring(body)
    except Exception:
        raise VeyError("invalid_xml", "消息格式无效") from None


def chunks_utf8(value: str, max_bytes=1800) -> list[str]:
    chunks, current, size = [], [], 0
    for char in value:
        width = len(char.encode())
        if size + width > max_bytes:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += width
    if current:
        chunks.append("".join(current))
    return chunks


class WeComSender:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=10, trust_env=False)
        self.token, self.expires_at = "", 0
        self.lock = asyncio.Lock()

    async def _token(self):
        async with self.lock:
            if self.token and self.expires_at > time.monotonic():
                return self.token
            response = await self.client.get(
                "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                params={
                    "corpid": self.settings.wecom_corp_id,
                    "corpsecret": self.settings.wecom_secret.get_secret_value(),
                },
            )
            response.raise_for_status()
            data = response.json()
            if data.get("errcode", 0) != 0 or not data.get("access_token"):
                raise VeyError("wecom_token_failed", "企微凭据或接口不可用")
            self.token, self.expires_at = (
                data["access_token"],
                time.monotonic() + max(1, data.get("expires_in", 7200) - 120),
            )
            return self.token

    async def send(self, user_id, body):
        token = await self._token()
        for chunk in chunks_utf8(redact(body)):
            response = await self.client.post(
                "https://qyapi.weixin.qq.com/cgi-bin/message/send",
                params={"access_token": token},
                json={
                    "touser": user_id,
                    "msgtype": "text",
                    "agentid": self.settings.wecom_agent_id,
                    "text": {"content": chunk},
                    "enable_duplicate_check": 1,
                    "duplicate_check_interval": 600,
                },
            )
            response.raise_for_status()
            data = response.json()
            if data.get("errcode", 0) != 0 or data.get("invaliduser"):
                if data.get("errcode") in {40014, 42001}:
                    self.token = ""
                raise VeyError("wecom_send_failed", "企微消息发送失败，已保留结果供重试")

    async def close(self):
        await self.client.aclose()
