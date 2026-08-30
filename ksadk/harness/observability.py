"""Harness 对平台/Studio 的稳定查询合同（长任务方案 §3.2 / §8 / P3）。

Studio 与控制面**不得解析 Harness 私有对象**（ContextManifest/CompactionRecord
dataclass、_EngineRun）。本模块把事件流投影为纯 dict 报告：

- :func:`context_trace`：每次模型调用的 manifest 快照（Section 构成 +
  Planned/Projected/Actual Token + usage 回填）；
- :func:`compaction_trace`：压缩历史（前后 Token、触发原因、质量校验、
  Memory Flush 候选引用）；
- :func:`token_report`：单 Run 的 Token 闭环汇总（可作门禁输入）。
- :func:`capability_health`：MCP / Skill / Sandbox 的统一健康快照。

输入是 ``RuntimeEvent`` 列表（server 已持久化的事实源），输出 JSON 兼容。
"""

from __future__ import annotations

from typing import Any, Sequence

from ksadk.harness.events import EventType, RuntimeEvent

_SKILL_TOOL_PREFIX = "skill_read_"
_SANDBOX_TOOL_PREFIX = "sandbox_"
_SANDBOX_CAPABILITY_REF = "sandbox://default@1"


def context_trace(events: Sequence[RuntimeEvent]) -> list[dict[str, Any]]:
    """context.built 序列 → manifest 快照列表。

    Actual 闭环（长任务方案 §6.2/§6.3）：usage.reported 事件 payload 带
    ``manifest_id``（引擎回填 Actual 时落账），据此把每次模型调用的实际
    Token 关联回对应 Manifest —— 可稳定查询「某次调用实际对应哪个 Manifest」。
    """
    usage_by_manifest: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.event_type == EventType.USAGE_REPORTED and event.payload.get("manifest_id"):
            usage_by_manifest[str(event.payload["manifest_id"])] = {
                "usage_event_id": event.event_id,
                "input_tokens": int(event.payload.get("input_tokens") or 0),
                "output_tokens": int(event.payload.get("output_tokens") or 0),
                "total_tokens": int(event.payload.get("total_tokens") or 0),
            }
    manifests: list[dict[str, Any]] = []
    for event in events:
        if event.event_type != EventType.CONTEXT_BUILT:
            continue
        payload = dict(event.payload)
        payload["seq_id"] = event.seq_id
        usage = usage_by_manifest.get(payload.get("manifest_id", ""))
        if usage is not None:
            payload["actual"] = usage
        manifests.append(payload)
    return manifests


def compaction_trace(events: Sequence[RuntimeEvent]) -> list[dict[str, Any]]:
    """context.compaction.completed 序列 → CompactionRecord 投影列表。"""
    records: list[dict[str, Any]] = []
    for event in events:
        if event.event_type != EventType.CONTEXT_COMPACTION_COMPLETED:
            continue
        payload = dict(event.payload)
        payload["seq_id"] = event.seq_id
        records.append(payload)
    return records


def token_report(events: Sequence[RuntimeEvent]) -> dict[str, Any]:
    """单 Run 的 Token 闭环汇总：Planned/Projected/Actual 与用量总计。"""
    manifests = context_trace(events)
    usages = [
        {
            "manifest_id": str(e.payload.get("manifest_id") or ""),
            "purpose": str(e.payload.get("purpose") or ""),
            "input_tokens": int(e.payload.get("input_tokens") or 0),
            "output_tokens": int(e.payload.get("output_tokens") or 0),
            "total_tokens": int(e.payload.get("total_tokens") or 0),
        }
        for e in events
        if e.event_type == EventType.USAGE_REPORTED
    ]
    return {
        "manifest_count": len(manifests),
        "planned_tokens": [m.get("planned_tokens") for m in manifests],
        "projected_tokens": [m.get("projected_tokens") for m in manifests],
        "actual": [m.get("actual") for m in manifests if m.get("actual")],
        "usage_events": usages,
        "actual_total_input_tokens": sum(u["input_tokens"] for u in usages),
        "actual_total_output_tokens": sum(u["output_tokens"] for u in usages),
        "compactions": len(compaction_trace(events)),
    }


def capability_health(events: Sequence[RuntimeEvent]) -> dict[str, Any]:
    """RuntimeEvent 序列 → 稳定的 Capability Health Snapshot。

    投影只依赖公开事件合同，不读取 MCP / Skill / Sandbox 的私有对象。
    MCP 的显式 ``capability.*`` 事件是权威状态；成功披露表示该
    MCP/Skill 在本 Run 中可用；Sandbox 以实际工具调用结果观测。

    安全边界：输出不包含原始异常文本，只保留稳定 ``reason_code``，
    避免内网地址、凭证或 Tool 返回泄露到 Studio。
    """
    states: dict[tuple[str, str], dict[str, Any]] = {}
    begins: dict[str, tuple[str, dict[str, Any]]] = {}

    def observe(
        event: RuntimeEvent,
        *,
        kind: str,
        capability_ref: str,
        status: str,
        operation: str,
        reason_code: str,
    ) -> None:
        if not capability_ref:
            return
        key = (kind, capability_ref)
        previous = states.get(key)
        transitions = int(previous.get("transition_count", 0)) if previous else 0
        if previous is None or previous["status"] != status:
            transitions += 1
        states[key] = {
            "capability_ref": capability_ref,
            "kind": kind,
            "status": status,
            "operation": operation,
            "reason_code": reason_code,
            "last_event_id": event.event_id,
            "last_seq_id": event.seq_id,
            "last_observed_at": event.timestamp,
            "run_id": event.run_id or event.invocation_id,
            "scope_id": event.scope_id or f"agent:{event.agent_id}",
            "transition_count": transitions,
        }

    for event in events:
        payload = event.payload
        if event.event_type == EventType.TOOL_CALL_BEGIN:
            begins[str(payload.get("call_id") or "")] = (
                str(payload.get("name") or ""),
                dict(payload.get("args") or {}),
            )
            continue

        if event.event_type in {
            EventType.CAPABILITY_DEGRADED,
            EventType.CAPABILITY_RECOVERED,
        }:
            status = (
                "degraded"
                if event.event_type == EventType.CAPABILITY_DEGRADED
                else "available"
            )
            observe(
                event,
                kind=str(payload.get("kind") or _kind_from_ref(payload.get("capability_ref"))),
                capability_ref=str(payload.get("capability_ref") or ""),
                status=status,
                operation=str(payload.get("operation") or "runtime_probe"),
                reason_code=_safe_reason_code(
                    payload.get("reason"),
                    default=(
                        "operation_failed"
                        if status == "degraded"
                        else "operation_succeeded"
                    ),
                ),
            )
            continue

        if event.event_type == EventType.MCP_DISCLOSED:
            observe(
                event,
                kind="mcp",
                capability_ref=str(payload.get("server_id") or ""),
                status="available",
                operation=f"disclosure_l{int(payload.get('level') or 0)}",
                reason_code="operation_succeeded",
            )
            continue

        if event.event_type == EventType.SKILL_DISCLOSED:
            observe(
                event,
                kind="skill",
                capability_ref=str(payload.get("skill_ref") or ""),
                status="available",
                operation=f"disclosure_l{int(payload.get('level') or 0)}",
                reason_code="operation_succeeded",
            )
            continue

        if event.event_type != EventType.TOOL_CALL_END:
            continue
        call_id = str(payload.get("call_id") or "")
        name, arguments = begins.pop(call_id, (str(payload.get("name") or ""), {}))
        error = payload.get("error")
        # 审批/策略拒绝是决策结果，不是 Capability 故障。
        if error and str(error).lower().startswith(("approval ", "policy-denied")):
            continue
        if name.startswith(_SKILL_TOOL_PREFIX):
            observe(
                event,
                kind="skill",
                capability_ref=str(arguments.get("skill_id") or ""),
                status="degraded" if error else "available",
                operation=name,
                reason_code=_tool_reason_code(error),
            )
        elif name.startswith(_SANDBOX_TOOL_PREFIX):
            observe(
                event,
                kind="sandbox",
                capability_ref=_SANDBOX_CAPABILITY_REF,
                status="degraded" if error else "available",
                operation=name,
                reason_code=_tool_reason_code(error),
            )

    items = sorted(states.values(), key=lambda item: (item["kind"], item["capability_ref"]))
    counts = {
        status: sum(1 for item in items if item["status"] == status)
        for status in ("available", "degraded", "unknown")
    }
    overall = "degraded" if counts["degraded"] else ("available" if items else "unknown")
    return {"overall_status": overall, "counts": counts, "items": items}


def _kind_from_ref(value: Any) -> str:
    prefix = str(value or "").partition("://")[0]
    return prefix if prefix in {"mcp", "skill", "sandbox", "builtin"} else "unknown"


def _safe_reason_code(value: Any, *, default: str) -> str:
    candidate = str(value or "").strip().lower()
    if candidate in {
        "operation_succeeded",
        "timeout",
        "transport_error",
        "circuit_open",
        "authentication_failed",
        "rate_limited",
        "unavailable",
    }:
        return candidate
    return default


def _tool_reason_code(error: Any) -> str:
    if not error:
        return "operation_succeeded"
    error_type = str(error).partition(":")[0].strip()
    safe = "".join(char for char in error_type if char.isalnum() or char in {"_", "-"})
    return (safe or "operation_failed")[:64]


__all__ = ["capability_health", "compaction_trace", "context_trace", "token_report"]
