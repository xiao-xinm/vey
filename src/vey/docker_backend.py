from __future__ import annotations

import codecs

import docker
import httpx

from vey.config import Service, Settings
from vey.domain import Action, ToolCall, VeyError, utcnow
from vey.security import safe_value


class DockerBackend:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = docker.DockerClient(
            base_url="unix:///var/run/docker.sock", timeout=settings.docker_timeout
        )

    def resolve(self, service: Service) -> list[dict]:
        project, name = service.key.split("/", 1)
        containers = self.client.containers.list(
            all=True,
            filters={
                "label": [
                    f"com.docker.compose.project={project}",
                    f"com.docker.compose.service={name}",
                ]
            },
        )
        return sorted([self._summary(c) for c in containers], key=lambda c: c["id"])

    def _summary(self, container) -> dict:
        attrs = container.attrs
        state = attrs.get("State", {})
        # Explicit fields only: never return Config.Env, mounts or the complete inspect response.
        return safe_value(
            {
                "id": container.id,
                "name": container.name,
                "status": state.get("Status", container.status),
                "health": state.get("Health", {}).get("Status", "not_configured"),
                "exit_code": state.get("ExitCode"),
                "restarts": attrs.get("RestartCount"),
                "image": attrs.get("Config", {}).get("Image"),
                "ports": attrs.get("NetworkSettings", {}).get("Ports", {}),
                "project": attrs.get("Config", {})
                .get("Labels", {})
                .get("com.docker.compose.project"),
            }
        )

    def inspect(self, container_id: str) -> dict:
        return self._summary(self.client.containers.get(container_id))

    def mutate(self, container_id: str, action: Action):
        container = self.client.containers.get(container_id)
        if action == Action.START:
            container.start()
        elif action == Action.STOP:
            container.stop(timeout=10)
        elif action == Action.RESTART:
            container.restart(timeout=10)
        else:
            raise VeyError("unsupported_action", "不支持此操作")

    def stats(self, container_id: str) -> dict:
        raw = self.client.containers.get(container_id).stats(stream=False)
        cpu = raw.get("cpu_stats", {})
        previous = raw.get("precpu_stats", {})
        used = cpu.get("cpu_usage", {}).get("total_usage", 0) - previous.get("cpu_usage", {}).get(
            "total_usage", 0
        )
        system = cpu.get("system_cpu_usage", 0) - previous.get("system_cpu_usage", 0)
        memory = raw.get("memory_stats", {})
        return {
            "id": container_id,
            "cpu_percent": round(used / system * cpu.get("online_cpus", 1) * 100, 2)
            if system > 0
            else None,
            "memory_bytes": memory.get("usage"),
            "memory_limit_bytes": memory.get("limit"),
            "sampled_at": utcnow().isoformat(),
        }

    def logs(self, ids: list[str], call: ToolCall) -> tuple[list[str], bool]:
        records: list[str] = []
        total = 0
        boundary = False
        for container_id in ids:
            source = self.client.containers.get(container_id).logs(
                stream=True,
                follow=False,
                timestamps=True,
                tail=self.settings.log_scan_lines,
                since=call.since,
                until=call.until or utcnow(),
            )
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            chunks = []
            try:
                for chunk in source:
                    total += len(chunk)
                    if total > self.settings.log_scan_bytes:
                        raise VeyError(
                            "log_scan_limit",
                            "日志扫描达到内部容量上限，请缩小时间范围；未返回不完整的伪分页结果",
                        )
                    chunks.append(decoder.decode(chunk))
                chunks.append(decoder.decode(b"", final=True))
            finally:
                close = getattr(source, "close", None)
                if close:
                    close()
            lines = "".join(chunks).splitlines()
            boundary = boundary or len(lines) >= self.settings.log_scan_lines
            for line in lines:
                if call.keyword is None or call.keyword.casefold() in line.casefold():
                    records.append(
                        f"{line[:30]} [{container_id[:12]}] {line[30:]}" if len(ids) > 1 else line
                    )
        # Docker timestamps lead each line; stable sort retains duplicate records.
        records.sort(key=lambda line: line.split(" ", 1)[0])
        return records, boundary

    def health(self, service: Service) -> dict:
        if not service.health_url:
            return {"status": "not_configured", "message": "未配置业务健康检查"}
        # URL comes only from the administrator's policy. Never follow redirects.
        try:
            with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
                with client.stream("GET", service.health_url) as response:
                    return {
                        "status": "passed" if 200 <= response.status_code < 300 else "failed",
                        "http_status": response.status_code,
                    }
        except httpx.HTTPError:
            return {"status": "unreachable"}

    def close(self):
        self.client.close()
