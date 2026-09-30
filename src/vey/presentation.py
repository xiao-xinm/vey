"""Deterministic, mobile-friendly summaries of collected evidence."""

import math
from datetime import datetime, timedelta, timezone


def _number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if math.isfinite(value) and value >= 0 else None
    return None


def _gib(value):
    return f"{value / 1024**3:.2f} GiB"


def render_system(result: dict) -> str:
    if not result.get("available"):
        return "服务器概况\n暂时无法采集宿主机信息，请检查采集配置。"
    lines = ["服务器概况"]
    try:
        sampled = datetime.fromisoformat(result["sampled_at"])
        if sampled.tzinfo is None:
            raise ValueError("Missing timezone")
        local = sampled.astimezone(timezone(timedelta(hours=8)))
        lines.append(f"采样时间：{local:%Y-%m-%d %H:%M:%S}（北京时间）")
    except (KeyError, TypeError, ValueError):
        lines.append("采样时间：暂不可用")

    cpu = _number(result.get("cpu_percent"))
    lines.append(f"CPU：{cpu:.1f}%（短时采样）" if cpu is not None else "CPU：暂不可用")
    load = str(result.get("loadavg") or "").split()[:3]
    lines.append(f"负载（1/5/15 分钟）：{' / '.join(load)}" if len(load) == 3 else "负载：暂不可用")
    try:
        seconds = float(str(result.get("uptime") or "").split()[0])
        if _number(seconds) is None:
            raise ValueError("Invalid uptime")
        days, remainder = divmod(int(seconds), 86400)
        hours, remainder = divmod(remainder, 3600)
        lines.append(f"已运行：{days} 天 {hours} 小时 {remainder // 60} 分钟")
    except (ValueError, IndexError):
        lines.append("运行时长：暂不可用")

    memory = result.get("memory") or {}
    total, available = _number(memory.get("MemTotal")), _number(memory.get("MemAvailable"))
    if total and available is not None and available <= total:
        used = total - available
        lines.append(f"内存：约 {_gib(used)} / {_gib(total)}（{used / total:.1%}）")
        lines.append(f"可用内存：{_gib(available)}")
    else:
        lines.append("内存：暂不可用")
    swap, free = _number(memory.get("SwapTotal")), _number(memory.get("SwapFree"))
    if swap == 0:
        lines.append("Swap：未配置")
    elif swap and free is not None and free <= swap:
        lines.append(f"Swap：已用 {_gib(swap - free)} / {_gib(swap)}")
    else:
        lines.append("Swap：暂不可用")

    lines.append("\n磁盘（已配置采集范围）")
    disks = result.get("disks") or []
    if not disks:
        lines.append("暂不可用")
    for index, disk in enumerate(disks, 1):
        total, available = _number(disk.get("total_bytes")), _number(disk.get("available_bytes"))
        if total and available is not None and available <= total:
            lines.append(f"磁盘 {index}：可用 {_gib(available)} / 总量 {_gib(total)}")
        else:
            lines.append(f"磁盘 {index}：暂不可用")

    lines.append("\n内存占用较高的进程（前 5 项）")
    processes = result.get("processes_by_memory") or []
    if not processes:
        lines.append("暂不可用")
    for process in processes[:5]:
        name = " ".join(str(process.get("name") or "未知进程").split())[:40]
        rss = _number(process.get("rss_kb"))
        size = f"{rss / 1024:.1f} MiB" if rss is not None else "暂不可用"
        lines.append(f"• {name}：{size}")
    lines.append("\n内存占用按总量减可用估算；进程统计为常驻内存。")
    return "\n".join(lines)
