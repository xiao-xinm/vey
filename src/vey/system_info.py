import os
import platform
from pathlib import Path

from vey.domain import utcnow


def collect_system(proc: Path | None, disks: list[str], labels=None, thresholds=None) -> dict:
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
    labels = labels or {}
    for name in ("loadavg", "uptime"):
        try:
            result[name] = (proc / name).read_text().strip()
        except OSError:
            result[name] = None
    try:
        result["cpu_count"] = sum(
            line.split()[0][3:].isdigit()
            for line in (proc / "stat").read_text().splitlines()
            if line.startswith("cpu")
        )
    except OSError:
        result["cpu_count"] = None
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
    try:
        entries = list(proc.iterdir())
    except OSError:
        entries = []
    for entry in entries:
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
                    "mount": labels.get(path, path),
                    "total_bytes": stat.f_blocks * stat.f_frsize,
                    "available_bytes": stat.f_bavail * stat.f_frsize,
                    "used_bytes": (stat.f_blocks - stat.f_bfree) * stat.f_frsize,
                }
            )
        except OSError:
            result["disks"].append(
                {"path": path, "mount": labels.get(path, path), "available": False}
            )
    # Inventory only persistent filesystems; do not bind-mount the host root to inspect files.
    try:
        mounts = []
        for line in (proc / "1/mountinfo").read_text().splitlines():
            before, after = line.split(" - ", 1)
            fields, fs = before.split(), after.split()[0]
            if fs in {"ext2", "ext3", "ext4", "xfs", "btrfs", "vfat", "zfs", "nfs", "nfs4", "cifs"}:
                mount = fields[4].replace(r"\040", " ").replace(r"\134", "\\")
                mounts.append(mount)
        result["unmapped_mounts"] = sorted(set(mounts) - set(labels.values()))
        result["mount_inventory_available"] = True
    except (OSError, ValueError, IndexError):
        result["mount_inventory_available"] = False
    result["alerts"] = system_alerts(result, thresholds) if thresholds else []
    return result


def system_alerts(result, thresholds):
    alerts = []
    cpu = result.get("cpu_percent")
    if cpu is not None and cpu >= thresholds.cpu_percent:
        alerts.append(f"CPU 采样 {cpu:.1f}% 达到阈值 {thresholds.cpu_percent:g}%")
    memory = result.get("memory") or {}
    total, available = memory.get("MemTotal"), memory.get("MemAvailable")
    if total and available is not None and 0 <= available <= total:
        percent = 100 * (total - available) / total
        if percent >= thresholds.memory_percent:
            alerts.append(f"内存使用约 {percent:.1f}% 达到阈值 {thresholds.memory_percent:g}%")
    for disk in result.get("disks", []):
        total, used = disk.get("total_bytes"), disk.get("used_bytes")
        if total and used is not None and 100 * used / total >= thresholds.disk_percent:
            alerts.append(
                f"磁盘 {disk['mount']} 已用 {100 * used / total:.1f}% 达到阈值 {thresholds.disk_percent:g}%"
            )
    try:
        load = float(result["loadavg"].split()[0])
        cores = result.get("cpu_count", 0)
        if cores and load / cores >= thresholds.load_per_cpu:
            alerts.append(
                f"1 分钟负载/CPU 核数 {load / cores:.2f} 达到阈值 {thresholds.load_per_cpu:g}"
            )
    except (KeyError, TypeError, ValueError, IndexError):
        pass
    return alerts
