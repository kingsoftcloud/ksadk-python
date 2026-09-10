"""Harness 对平台/Studio 的稳定查询合同（长任务方案 §3.2 / §8 / P3）。

Studio 与控制面**不得解析 Harness 私有对象**（ContextManifest/CompactionRecord
dataclass、_EngineRun）。本模块把事件流投影为纯 dict 报告：

- :func:`context_trace`：每次模型调用的 manifest 快照（Section 构成 +
  Planned/Projected/Actual Token + usage 回填）；
- :func:`compaction_trace`：压缩历史（前后 Token、触发原因、质量校验、
  Memory Flush 候选引用）；
- :func:`token_report`：单 Run 的 Token 闭环汇总（可作门禁输入）。
- :func:`context_inspection`：面向 Studio 的单页 Context 可视化投影；
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
                "cached_tokens": int(event.payload.get("cached_tokens") or 0),
                "reasoning_tokens": int(event.payload.get("reasoning_tokens") or 0),
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
            "cached_tokens": int(e.payload.get("cached_tokens") or 0),
            "reasoning_tokens": int(e.payload.get("reasoning_tokens") or 0),
        }
        for e in events
        if e.event_type == EventType.USAGE_REPORTED
    ]
    input_total = sum(u["input_tokens"] for u in usages)
    cached_total = sum(u["cached_tokens"] for u in usages)
    cache_diagnostics = [
        {**dict(e.payload), "seq_id": e.seq_id, "event_id": e.event_id}
        for e in events
        if e.event_type == EventType.PROMPT_CACHE_DIAGNOSTIC
    ]
    return {
        "manifest_count": len(manifests),
        "planned_tokens": [m.get("planned_tokens") for m in manifests],
        "projected_tokens": [m.get("projected_tokens") for m in manifests],
        "actual": [m.get("actual") for m in manifests if m.get("actual")],
        "usage_events": usages,
        "actual_total_input_tokens": input_total,
        "actual_total_output_tokens": sum(u["output_tokens"] for u in usages),
        "actual_total_cached_tokens": cached_total,
        "actual_total_reasoning_tokens": sum(u["reasoning_tokens"] for u in usages),
        "provider_cache_hit_ratio": cached_total / input_total if input_total else 0.0,
        "prompt_cache_diagnostics": cache_diagnostics,
        "prompt_cache_breaks": sum(bool(d.get("cache_break")) for d in cache_diagnostics),
        "compactions": len(compaction_trace(events)),
    }


def context_inspection(events: Sequence[RuntimeEvent]) -> dict[str, Any]:
    """把分散的 Context 事件投影为 Studio 可直接渲染的安全报告。

    该合同刻意不返回 Prompt/消息/Memory/Tool 正文，也不返回召回 query、
    关键事实原文或底层异常文本。Studio 只需展示预算、Section 构成、压缩
    质量、缓存、召回与渐进披露进度，不应重新解释 Harness 私有状态。
    """
    manifests = context_trace(events)
    token_usage = token_report(events)
    compactions = compaction_trace(events)
    planned = [event for event in events if event.event_type == EventType.CONTEXT_PLANNED]
    recoveries = [event for event in events if event.event_type == EventType.CONTEXT_RECOVERED]
    recalls = [event for event in events if event.event_type == EventType.MEMORY_RECALLED]

    latest_manifest = manifests[-1] if manifests else {}
    latest_plan = dict(planned[-1].payload) if planned else {}
    actual = dict(latest_manifest.get("actual") or {})
    planned_tokens = int(latest_manifest.get("planned_tokens") or 0)
    projected_tokens = int(latest_manifest.get("projected_tokens") or 0)
    actual_input = int(actual.get("input_tokens") or 0)
    budget_tokens = int(
        latest_plan.get("budget_tokens")
        or latest_manifest.get("context_window_tokens")
        or 0
    )
    section_source = latest_plan.get("sections") or {}
    sections = [
        {"kind": str(kind), "tokens": int(tokens or 0)}
        for kind, tokens in sorted(dict(section_source).items())
    ]

    quality_failures: list[dict[str, Any]] = []
    total_before = 0
    total_after = 0
    for record in compactions:
        total_before += int(record.get("before_tokens") or 0)
        total_after += int(record.get("after_tokens") or 0)
        for check, passed in dict(record.get("quality_checks") or {}).items():
            if not passed:
                quality_failures.append(
                    {
                        "compaction_id": str(record.get("compaction_id") or ""),
                        "check": str(check),
                    }
                )

    memory_items = [item for event in recalls for item in event.payload.get("items") or ()]
    skill_disclosures = [
        event for event in events if event.event_type == EventType.SKILL_DISCLOSED
    ]
    mcp_disclosures = [
        event for event in events if event.event_type == EventType.MCP_DISCLOSED
    ]
    warnings: list[dict[str, str]] = []
    if quality_failures:
        warnings.append(
            {
                "code": "compaction_quality_failed",
                "message": "至少一次压缩质量检查未通过",
            }
        )
    if recoveries:
        warnings.append(
            {
                "code": "context_recovered",
                "message": "本次运行发生过 Context 降级或恢复",
            }
        )
    if budget_tokens and max(projected_tokens, actual_input) > budget_tokens:
        warnings.append(
            {
                "code": "context_budget_exceeded",
                "message": "模型输入超过规划预算",
            }
        )

    return {
        "schema_version": 1,
        "status": "warning" if warnings else "healthy",
        "current": {
            "manifest_id": str(latest_manifest.get("manifest_id") or ""),
            "supersedes": str(latest_manifest.get("supersedes") or ""),
            "stable_prompt_hash": str(latest_manifest.get("stable_prompt_hash") or ""),
            "budget_tokens": budget_tokens,
            "planned_tokens": planned_tokens,
            "projected_tokens": projected_tokens,
            "actual_input_tokens": actual_input,
            "actual_output_tokens": int(actual.get("output_tokens") or 0),
            "utilization_ratio": (
                max(projected_tokens, actual_input) / budget_tokens if budget_tokens else 0.0
            ),
            "sections": sections,
        },
        "cache": {
            "provider_hit_ratio": token_usage["provider_cache_hit_ratio"],
            "cached_tokens": token_usage["actual_total_cached_tokens"],
            "diagnostic_count": len(token_usage["prompt_cache_diagnostics"]),
            "break_count": token_usage["prompt_cache_breaks"],
        },
        "compaction": {
            "count": len(compactions),
            "input_tokens": total_before,
            "output_tokens": total_after,
            "saved_tokens": max(total_before - total_after, 0),
            "quality_failures": quality_failures,
            "reinjected_fact_count": sum(
                len(record.get("reinjected_critical_facts") or ()) for record in compactions
            ),
        },
        "memory": {
            "recall_count": len(recalls),
            "candidate_count": len(memory_items),
            "injected_count": sum(bool(item.get("injected")) for item in memory_items),
        },
        "disclosure": {
            "skills": _disclosure_summary(skill_disclosures, ref_field="skill_ref"),
            "mcp": _disclosure_summary(mcp_disclosures, ref_field="server_id"),
        },
        "recoveries": {
            "count": len(recoveries),
            "reason_codes": sorted(
                {_safe_recovery_reason(event.payload.get("reason")) for event in recoveries}
            ),
        },
        "warnings": warnings,
    }


def _disclosure_summary(
    events: Sequence[RuntimeEvent], *, ref_field: str
) -> dict[str, Any]:
    levels = {str(level): 0 for level in range(1, 4)}
    refs: set[str] = set()
    for event in events:
        level = str(int(event.payload.get("level") or 0))
        if level in levels:
            levels[level] += 1
        ref = str(event.payload.get(ref_field) or "")
        if ref:
            refs.add(ref)
    return {"resource_count": len(refs), "events": len(events), "levels": levels}


def _safe_recovery_reason(value: Any) -> str:
    reason = str(value or "unknown").partition(":")[0].strip().lower()
    safe = "".join(char for char in reason if char.isalnum() or char in {"_", "-"})
    return (safe or "unknown")[:64]


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
    sandbox_capability_ref = _SANDBOX_CAPABILITY_REF

    def observe(
        event: RuntimeEvent,
        *,
        kind: str,
        capability_ref: str,
        status: str,
        operation: str,
        reason_code: str,
        metadata: dict[str, Any] | None = None,
        declaration: bool = False,
    ) -> None:
        if not capability_ref:
            return
        key = (kind, capability_ref)
        previous = states.get(key)
        run_id = event.run_id or event.invocation_id
        if declaration and previous is not None:
            previous.update(metadata or {})
            previous["last_declared_event_id"] = event.event_id
            previous["last_declared_at"] = event.timestamp
            return

        changed = previous is None or previous["status"] != status
        history = list(previous.get("history", ())) if previous else []
        if changed:
            history.append(
                {
                    "status": status,
                    "operation": operation,
                    "reason_code": reason_code,
                    "event_id": event.event_id,
                    "seq_id": event.seq_id,
                    "timestamp": event.timestamp,
                    "run_id": run_id,
                }
            )
            history = history[-20:]

        degraded_runs = set(previous.get("_degraded_run_ids", ())) if previous else set()
        alert_status = str(previous.get("alert_status") or "none") if previous else "none"
        degraded_since = previous.get("degraded_since") if previous else None
        recovered_at = previous.get("recovered_at") if previous else None
        recovery_confirmed = bool(previous.get("recovery_confirmed")) if previous else False
        if status == "degraded":
            degraded_runs.add(run_id)
            if alert_status != "open":
                degraded_since = event.timestamp
            alert_status = "open"
            recovered_at = None
            recovery_confirmed = False
        elif status == "available":
            if previous and (
                previous.get("status") == "degraded" or previous.get("alert_status") == "open"
            ):
                alert_status = "resolved"
                recovered_at = event.timestamp
                recovery_confirmed = True
            elif alert_status != "resolved":
                alert_status = "none"
                recovered_at = None
                recovery_confirmed = False
            degraded_runs.clear()

        item = {
            "capability_ref": capability_ref,
            "kind": kind,
            "status": status,
            "operation": operation,
            "reason_code": reason_code,
            "last_event_id": event.event_id,
            "last_seq_id": event.seq_id,
            "last_observed_at": None if declaration else event.timestamp,
            "run_id": run_id,
            "scope_id": event.scope_id or f"agent:{event.agent_id}",
            "transition_count": len(history),
            "history": history,
            "alert_status": alert_status,
            "degraded_since": degraded_since,
            "recovered_at": recovered_at,
            "recovery_confirmed": recovery_confirmed,
            "consecutive_degraded_runs": len(degraded_runs),
            "_degraded_run_ids": sorted(degraded_runs),
        }
        if previous:
            for field in ("required", "load_policy", "last_declared_event_id", "last_declared_at"):
                if field in previous:
                    item[field] = previous[field]
        item.update(metadata or {})
        states[key] = item

    for event in events:
        payload = event.payload
        if event.event_type == EventType.TOOL_CALL_BEGIN:
            begins[str(payload.get("call_id") or "")] = (
                str(payload.get("name") or ""),
                dict(payload.get("args") or {}),
            )
            continue

        if event.event_type == EventType.CAPABILITY_DECLARED:
            declared_ref = str(payload.get("capability_ref") or "")
            declared_kind = str(payload.get("kind") or _kind_from_ref(declared_ref))
            if declared_kind == "sandbox" and declared_ref:
                sandbox_capability_ref = declared_ref
            observe(
                event,
                kind=declared_kind,
                capability_ref=declared_ref,
                status="unknown",
                operation="declared",
                reason_code="not_observed",
                metadata={
                    "required": bool(payload.get("required", True)),
                    "load_policy": str(payload.get("load_policy") or "on_demand"),
                    "last_declared_event_id": event.event_id,
                    "last_declared_at": event.timestamp,
                },
                declaration=True,
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
                capability_ref=sandbox_capability_ref,
                status="degraded" if error else "available",
                operation=name,
                reason_code=_tool_reason_code(error),
            )

    items = []
    for item in sorted(states.values(), key=lambda value: (value["kind"], value["capability_ref"])):
        public_item = dict(item)
        public_item.pop("_degraded_run_ids", None)
        items.append(public_item)
    counts = {
        status: sum(1 for item in items if item["status"] == status)
        for status in ("available", "degraded", "unknown")
    }
    overall = (
        "degraded"
        if counts["degraded"]
        else ("available" if counts["available"] else "unknown")
    )
    return {
        "overall_status": overall,
        "counts": counts,
        "open_alerts": sum(1 for item in items if item["alert_status"] == "open"),
        "resolved_alerts": sum(1 for item in items if item["alert_status"] == "resolved"),
        "items": items,
    }


def _kind_from_ref(value: Any) -> str:
    prefix = str(value or "").partition("://")[0]
    return prefix if prefix in {"mcp", "skill", "sandbox", "builtin"} else "unknown"


def _safe_reason_code(value: Any, *, default: str) -> str:
    candidate = str(value or "").strip().lower()
    if candidate in {
        "not_observed",
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


__all__ = [
    "capability_health",
    "compaction_trace",
    "context_inspection",
    "context_trace",
    "token_report",
]
