"""Bounded read-only loop shared by production and isolated trajectory evaluation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from vey.domain import Diagnosis, ToolCall, VeyError, digest
from vey.security import safe_value

LOOP_VERSION = "diagnosis-v1"
STOP_LABELS = {
    "concluded": "已根据现有证据结束检查；本轮仅执行只读查询，未实施修复。",
    "tool_budget": "已达到 5 次工具调用上限，停止继续排查。",
    "duplicate": "检测到重复检查，已停止继续排查。",
    "timeout": "已达到检查时间上限，停止继续排查。",
    "model_error": "模型响应失败，已停止继续排查。",
    "invalid_conclusion": "结论缺少结构化证据或引用无效，已停止检查并保留已有证据。",
    "invalid_tool": "规划的工具或目标不在许可范围，已停止继续排查。",
}
CAPABILITIES = {
    "system": "宿主机当前资源快照",
    "services": "已登记服务列表",
    "inspect": "容器当前状态和最近一次退出信息",
    "stats": "容器实时资源采样（不提供历史曲线）",
    "ranking": "已登记容器当前资源排行",
    "logs": "有界容器日志",
    "health": "管理员预设业务健康地址的当前响应",
}


@dataclass
class DiagnosisResult:
    stop_reason: str
    evidence: list[dict]
    diagnosis: Diagnosis | None = None
    trace: list[dict] = field(default_factory=list)
    model_calls: int = 0
    tool_calls: int = 0

    def as_dict(self):
        return safe_value(
            {
                "loop_version": LOOP_VERSION,
                "stop_reason": self.stop_reason,
                "evidence": self.evidence,
                "diagnosis": self.diagnosis.model_dump(mode="json") if self.diagnosis else None,
                "trace": self.trace,
                "model_calls": self.model_calls,
                "tool_calls": self.tool_calls,
            }
        )

    def summary(self):
        lines = [STOP_LABELS[self.stop_reason]]
        if self.diagnosis:
            for h in self.diagnosis.hypotheses:
                certainty = "有证据支持的假设" if h.confidence == "supported" else "待验证假设"
                lines.append(f"{certainty}：{h.statement}（{', '.join(h.evidence_refs)}）")
            lines.append("尚未确定：" + self.diagnosis.uncertainty)
            for v in self.diagnosis.verification:
                # The capability is deterministic; never present a model's promise of historical data.
                lines.append(
                    f"可继续查询：{CAPABILITIES[v.tool.name]}，目标 {v.tool.target or '本机范围'}。"
                )
        return "\n".join(lines)


def permitted(call: ToolCall, catalog: list[dict]) -> bool:
    targets = {s["key"] for s in catalog}
    return not call.cursor and (
        call.target in targets if call.target else call.name in {"system", "services", "ranking"}
    )


def valid_diagnosis(diagnosis: Diagnosis | None, evidence: list[dict], catalog: list[dict]):
    if diagnosis is None:
        return False
    ids = {e["id"] for e in evidence}
    return all(set(h.evidence_refs).issubset(ids) for h in diagnosis.hypotheses) and all(
        permitted(v.tool, catalog) for v in diagnosis.verification
    )


async def run_diagnosis(
    model, message, target, catalog, read, *, evidence=None, before_step=None, timeout=60
):
    """At most five reads/five planner calls. Errors count; no hidden retry/final call.

    read is a capability returning one sanitized tool result; it must not mutate
    evidence. before_step validates live session state and may abort the task.
    Caller cancellation propagates; only this loop's own timeout is summarized.
    """
    if not 0 < timeout <= 60:
        raise ValueError("diagnosis timeout must be in (0, 60]")
    result = DiagnosisResult("tool_budget", evidence if evidence is not None else [])
    seen = set()
    try:
        async with asyncio.timeout(timeout):
            for _ in range(5):
                if before_step:
                    before_step()
                result.model_calls += 1
                try:
                    step = await model.next_step(message, result.evidence, target, catalog)
                except VeyError:
                    result.stop_reason = "model_error"
                    break
                if before_step:
                    before_step()
                result.trace.append(safe_value(step.model_dump(mode="json")))
                if step.tool is None:
                    if valid_diagnosis(step.diagnosis, result.evidence, catalog):
                        result.diagnosis = step.diagnosis
                        result.stop_reason = "concluded"
                    else:
                        result.stop_reason = "invalid_conclusion"
                    break
                if not permitted(step.tool, catalog):
                    result.stop_reason = "invalid_tool"
                    break
                fingerprint = digest(step.tool.model_dump(mode="json"))
                if fingerprint in seen:
                    result.stop_reason = "duplicate"
                    break
                seen.add(fingerprint)
                result.tool_calls += 1
                item = {
                    "id": f"E{len(result.evidence) + 1}",
                    "tool": step.tool.model_dump(mode="json"),
                }
                try:
                    item["result"] = safe_value(await read(step.tool))
                except VeyError as error:
                    # Session/auth revocation aborts rather than becoming model evidence.
                    if error.code in {
                        "invalid_session",
                        "expired_session",
                        "unauthorized",
                        "unauthorized_user",
                        "task_cancelled",
                        "invalid_actor",
                        "stale_session",
                    }:
                        raise
                    item.update(error=error.code, message=error.message)
                result.evidence.append(safe_value(item))
    except TimeoutError:
        result.stop_reason = "timeout"
    return result
