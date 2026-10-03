"""Deterministic, mobile-friendly summaries of collected evidence."""

import math
import re
from datetime import datetime, timedelta, timezone

from vey.security import redact


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
        label = disk.get("mount") or f"磁盘 {index}"
        total, available = _number(disk.get("total_bytes")), _number(disk.get("available_bytes"))
        if total and available is not None and available <= total:
            lines.append(f"{label}：可用 {_gib(available)} / 总量 {_gib(total)}")
            used = _number(disk.get("used_bytes"))
            if used is not None:
                lines.append(f"  已用 {_gib(used)}（{used / total:.1%}）")
        else:
            lines.append(f"{label}：暂不可用")
    if result.get("unmapped_mounts"):
        lines.append("未配置容量采集：" + "、".join(result["unmapped_mounts"]))
    if result.get("mount_inventory_available") is False:
        lines.append("宿主机挂载清单不可读取，无法确认采集范围是否完整。")

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
    if "alerts" in result:
        lines.append("\n阈值提示（本次采样）")
        lines.extend(result["alerts"] or ["已取得的指标未触发配置阈值；缺失指标不作健康判断。"])
        lines.append("瞬时数据不能证明长期趋势；输入「资源排行」查看容器占用。")
    for item in result.get("container_anomalies", []):
        lines.append(
            f"容器提示：{item['target']} / {item['name']}：{state(item.get('status'))}，{health(item.get('health'))}"
        )
    return "\n".join(lines)


def state(value):
    return {
        "running": "运行中",
        "exited": "已停止",
        "created": "已创建未启动",
        "restarting": "重启中",
        "paused": "已暂停",
        "dead": "异常终止",
    }.get(value, "状态未知")


def health(value):
    return {
        "healthy": "容器健康检查通过",
        "unhealthy": "容器健康检查失败",
        "starting": "健康检查启动中",
        "not_configured": "未配置健康检查",
        "passed": "业务健康检查通过",
        "failed": "业务健康检查失败",
        "unreachable": "业务健康地址不可达",
        "not_checked": "未执行业务健康检查",
    }.get(value, "健康状态未知")


def status(value):
    return {
        "succeeded": "已完成",
        "failed": "失败",
        "partial": "部分完成，验证未全部通过",
        "unknown": "结果未知，需核对，勿重复操作",
        "cancelled": "已取消",
        "expired": "已过期",
        "pending": "等待确认",
        "awaiting_confirmation": "等待确认",
        "executing": "执行中",
        "running": "处理中",
        "queued": "排队中",
        "control_queued": "排队中",
        "not_found": "未找到操作记录",
        "reconciled": "管理员已核对",
    }.get(value, "状态未知")


def render_logs(result, expanded=False):
    raw = redact(result.get("text", ""))[:10000]
    records = raw.splitlines()
    title = f"{result.get('target', '')} · 日志{'原文' if expanded else '摘要'}"
    if not records:
        return title + "\n本次查询没有匹配日志。\n" + result.get("message", "")
    if expanded:
        return f"{title}（本页快照）\n{raw}\n{result.get('message', '')}"
    errors = [
        i
        for i, line in enumerate(records)
        if re.search(r"\b(error|fatal|panic|exception)\b|错误|异常", line, re.I)
    ]
    warnings = [
        i for i, line in enumerate(records) if re.search(r"\bwarn(?:ing)?\b|警告", line, re.I)
    ]
    lines = [
        title,
        f"本页 {len(records)} 行，{len(raw)} 字符；错误类关键词 {len(errors)} 行，警告类 {len(warnings)} 行。",
        "以上为文本匹配统计，不代表已确认故障或根因。",
        "关键原文（最多 5 行，每行最多 240 字符）：",
    ]
    selected = sorted(
        set((errors + warnings + list(range(max(0, len(records) - 5), len(records))))[:5])
    )
    for index in selected:
        line = records[index]
        lines.append(f"第 {index + 1} 行：{line[:240]}" + ("…（摘录）" if len(line) > 240 else ""))
    lines.append(result.get("message", ""))
    lines.append("发送「展开日志」查看本页完整原文；「下一页」查看后续分页内容。")
    return "\n".join(lines)


def render_instances(result):
    rows = result.get("instances", [])
    lines = [f"{result.get('target', '服务状态')} · {len(rows)} 个实例"]
    if not rows:
        lines.append("未发现已部署容器。")
    for row in rows:
        lines.append(
            f"• {row.get('name') or row.get('id', '')[:12]}：{state(row.get('status'))}；{health(row.get('health'))}"
        )
        lines.append(
            f"  ID {row.get('id', '')[:12]}；重启次数 {row.get('restarts', '未知')}；退出码 {row.get('exit_code', '未知')}"
        )
        if row.get("image"):
            lines.append(f"  镜像：{row['image']}")
    return "\n".join(lines)


def render_stats(result):
    ranking = "ranking" in result
    rows = result.get("ranking" if ranking else "instances", [])
    lines = [
        "容器资源排行 · " + ("CPU" if result.get("sort_by") == "cpu" else "内存")
        if ranking
        else f"{result.get('target', '')} · 容器资源"
    ]
    for index, row in enumerate(rows[:10], 1):
        memory, cpu = _number(row.get("memory_bytes")), _number(row.get("cpu_percent"))
        mem = f"{memory / 1024**2:.1f} MiB" if memory is not None else "暂不可用"
        cpu_text = f"{cpu:.1f}%" if cpu is not None else "暂不可用"
        lines.append(
            f"{index}. {row.get('name') or row.get('id', '')[:12]}：内存 {mem}；CPU {cpu_text}"
        )
    if not rows:
        lines.append("没有取得可用的容器采样数据。")
    if len(rows) > 10:
        lines.append(f"已采集 {len(rows)} 个容器，仅展示前 10 项。")
    for failure in result.get("failures", []):
        lines.append(f"未采集 {failure['id']}：{failure['reason']}")
    if result.get("skipped"):
        lines.append(f"达到采集预算，跳过 {result['skipped']} 个容器；当前不是完整排行。")
    if result.get("sampled_at"):
        lines.append(f"采样结束：{result['sampled_at']}")
    lines.append(
        "范围为已配置目标；逐个瞬时采样。CPU 100% 表示一核；内存为 Docker usage，包含缓存。"
    )
    return "\n".join(lines)


def render_operation(result):
    action = {"start": "启动", "stop": "停止", "restart": "重启"}.get(result.get("action"), "操作")
    lines = [f"{result.get('target', '')} · {action}", f"结果：{status(result.get('status'))}"]
    if result.get("task_id"):
        lines.append(f"操作编号：{result['task_id']}")
    details = result.get("result") or {}
    for item in details.get("instances", []):
        label = item.get("id", "")[:12]
        if item.get("operation") == "unknown":
            lines.append(f"• {label}：执行结果未知，未自动重试。")
        else:
            observed = item.get("verification") or {}
            verdict = "达到预期状态" if item.get("expected_state_observed") else "未达到预期状态"
            lines.append(
                f"• {label}：{state(observed.get('status'))}；{verdict}；{health(observed.get('health'))}"
            )
    if details.get("business_health"):
        lines.append(health(details["business_health"].get("status")))
    if details.get("note") or result.get("note"):
        lines.append(details.get("note") or result.get("note"))
    if isinstance(result.get("current_state"), list):
        lines.append("当前观察（不能证明历史操作是否执行）：")
        lines.append(render_instances({"instances": result["current_state"]}))
    return "\n".join(lines)


def render_readable(result, tool_name=None):
    if tool_name == "system":
        return render_system(result)
    if "text" in result:
        return render_logs(result)
    if "task_id" in result and "action" in result:
        return render_operation(result)
    if "id" in result and "result" in result and "status" in result:
        return f"任务编号：{result['id']}\n状态：{status(result['status'])}\n{result['result']}"
    if tool_name == "health":
        code = result.get("http_status")
        return f"{result.get('target', '')}：{health(result.get('status'))}" + (
            f"（HTTP {code}）" if code else ""
        )
    if "services" in result:
        return "服务列表\n" + "\n\n".join(render_instances(s) for s in result["services"])
    if tool_name in {"stats", "ranking"} or "ranking" in result:
        return render_stats(result)
    if "instances" in result and isinstance(result["instances"], list):
        return render_instances(result)
    if "status" in result:
        return status(result["status"]) + (
            "\n" + result["message"] if result.get("message") else ""
        )
    return "未取得可展示的结果，请使用任务编号核对。"
