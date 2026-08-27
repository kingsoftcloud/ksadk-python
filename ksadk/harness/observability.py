"""Harness 对平台/Studio 的稳定查询合同（长任务方案 §3.2 / §8 / P3）。

Studio 与控制面**不得解析 Harness 私有对象**（ContextManifest/CompactionRecord
dataclass、_EngineRun）。本模块把事件流投影为纯 dict 报告：

- :func:`context_trace`：每次模型调用的 manifest 快照（Section 构成 +
  Planned/Projected/Actual Token + usage 回填）；
- :func:`compaction_trace`：压缩历史（前后 Token、触发原因、质量校验、
  Memory Flush 候选引用）；
- :func:`token_report`：单 Run 的 Token 闭环汇总（可作门禁输入）。

输入是 ``RuntimeEvent`` 列表（server 已持久化的事实源），输出 JSON 兼容。
"""

from __future__ import annotations

from typing import Any, Sequence

from ksadk.harness.events import EventType, RuntimeEvent


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


__all__ = ["compaction_trace", "context_trace", "token_report"]
