"""Dashboard's narrow management client: no operation credential or Docker access."""

import httpx

from vey.domain import VeyError


class ConfigurationClient:
    def __init__(self, socket, token):
        if len(token) < 32:
            raise ValueError("Independent configuration token required")
        self.client = httpx.Client(
            transport=httpx.HTTPTransport(uds=socket),
            base_url="http://executor",
            timeout=30,
            headers={"Authorization": "Bearer " + token},
        )

    def call(self, action="", payload=None):
        if action not in {"", "/preview", "/publish"}:
            raise ValueError("Unsupported management route")
        try:
            response = self.client.request(
                "GET" if payload is None else "POST", "/management/config" + action, json=payload
            )
            if response.status_code >= 500:
                raise VeyError(
                    "configuration_unavailable",
                    "配置服务暂不可用；发布结果请刷新版本核对，不要自动重试",
                    503,
                )
            data = response.json()
            if response.status_code != 200:
                raise VeyError(
                    data.get("error", "configuration_rejected"),
                    data.get("message", "配置请求未通过校验"),
                    response.status_code,
                )
            return data
        except (httpx.HTTPError, ValueError):
            raise VeyError(
                "configuration_unavailable",
                "配置服务连接异常；如刚提交发布，请刷新版本核对结果",
                503,
            ) from None

    def close(self):
        self.client.close()
