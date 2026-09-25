import os
import platform
from pathlib import Path

from vey.domain import utcnow


def collect_system(proc: Path | None, disks: list[str]) -> dict:
    if proc is None or platform.system() != "Linux":
        return {
            "available": False,
            "reason": "未配置 Ubuntu 宿主机只读 proc 来源；不返回容器指标冒充宿主机",
        }
    result = {
        "available": True,
        "source": "configured_host_proc",
        "sampled_at": utcnow().isoformat(),
    }
    for name in ("loadavg", "uptime"):
        try:
            result[name] = (proc / name).read_text().strip()
        except OSError:
            result[name] = None
    try:
        memory = {}
        for line in (proc / "meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
                memory[key] = int(value.split()[0]) * 1024
        result["memory"] = memory
    except (OSError, ValueError):
        result["memory"] = None
    import time

    def cpu_sample():
        return [int(v) for v in (proc / "stat").read_text().splitlines()[0].split()[1:9]]

    try:
        start = cpu_sample()
        time.sleep(0.15)
        end = cpu_sample()
        delta = [b - a for a, b in zip(start, end, strict=True)]
        result["cpu_percent"] = (
            round(100 * (1 - (delta[3] + delta[4]) / sum(delta)), 2) if sum(delta) else 0
        )
        result["cpu_sample_seconds"] = 0.15
    except (OSError, ValueError, IndexError):
        result["cpu_percent"] = None
    processes = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = {}
            for line in (entry / "status").read_text().splitlines():
                key, _, value = line.partition(":")
                if key in {"Name", "VmRSS"}:
                    status[key] = value.strip()
            processes.append(
                {
                    "pid": int(entry.name),
                    "name": status.get("Name"),
                    "rss_kb": int(status.get("VmRSS", "0").split()[0]),
                }
            )
        except (OSError, ValueError):
            continue
    result["processes_by_memory"] = sorted(processes, key=lambda p: p["rss_kb"], reverse=True)[:10]
    result["disks"] = []
    for path in disks:
        try:
            stat = os.statvfs(path)
            result["disks"].append(
                {
                    "path": path,
                    "total_bytes": stat.f_blocks * stat.f_frsize,
                    "available_bytes": stat.f_bavail * stat.f_frsize,
                }
            )
        except OSError:
            result["disks"].append({"path": path, "available": False})
    return result
