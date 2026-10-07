import httpx

from vey.domain import Actor, ConfirmRequest, PrepareRequest, ReadRequest, VeyError


class ExecutorClient:
    def __init__(self, settings, client=None):
        self.client = client or httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=settings.executor_socket),
            base_url="http://executor",
            timeout=30,
        )
        self.headers = {"Authorization": "Bearer " + settings.executor_token.get_secret_value()}

    async def post(self, path, payload):
        try:
            response = await self.client.post(
                path, json=payload.model_dump(mode="json"), headers=self.headers
            )
        except httpx.HTTPError:
            raise VeyError(
                "executor_unavailable", "执行器不可用或响应超时；修改操作不得自动重试", 503
            ) from None
        if response.status_code >= 500:
            raise VeyError(
                "executor_unavailable", "执行器响应异常，修改请求结果需核对，禁止自动重试", 503
            )
        if response.status_code != 200:
            try:
                data = response.json()
            except ValueError:
                data = {}
            raise VeyError(
                data.get("error", "executor_error"),
                data.get("message", "执行器拒绝请求"),
                response.status_code,
            )
        return response.json()

    async def sync_actor(self, actor: Actor):
        return await self.post("/session", actor)

    async def policy(self):
        try:
            response = await self.client.get("/policy", headers=self.headers)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise VeyError("executor_unavailable", "当前服务配置不可用，停止执行", 503) from None

    async def read(self, request: ReadRequest):
        return await self.post("/read", request)

    async def prepare(self, request: PrepareRequest):
        return await self.post("/prepare", request)

    async def confirm(self, request: ConfirmRequest):
        return await self.post("/confirm", request)

    async def cancel(self, request: ConfirmRequest):
        return await self.post("/cancel", request)

    async def status(self, actor, task_id):
        return await self.post(f"/operations/{task_id}", actor)

    async def close(self):
        await self.client.aclose()
