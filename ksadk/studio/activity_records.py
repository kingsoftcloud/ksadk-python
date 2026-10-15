"""Human-readable activity drilldown derived from durable execution evidence.

This projection never reads model reasoning or tool arguments/results. Old
records retain honest action counts; richer records may attach an explicitly
public action description. Neither path rewrites the canonical event log.
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_TERMINAL = {"completed", "failed", "cancelled", "interrupted", "timed_out"}
_STATUS = {
    "accepted": "running",
    "started": "running",
    "running": "running",
    "in_progress": "running",
    "succeeded": "completed",
    "completed": "completed",
    "done": "completed",
    "failed": "failed",
    "error": "failed",
    "canceled": "cancelled",
    "cancelled": "cancelled",
    "interrupted": "unknown",
}
_ACTIONS = {
    "web_search": ("search", "搜索资料"),
    "web_fetch": ("fetch", "查看资料"),
    "read_workspace_file": ("read", "读取文件"),
    "list_workspace_files": ("read", "查看工作区"),
    "write_workspace_file": ("write", "编辑并保存文件"),
    "execute_command": ("command", "运行命令"),
    "run_command": ("command", "运行命令"),
    "exec_command": ("command", "运行命令"),
    "run_workspace_command": ("command", "运行命令"),
    "edit_workspace_file": ("write", "编辑并保存文件"),
    # Codex native item kinds share the same activity vocabulary.
    "commandExecution": ("command", "运行命令"),
    "mcpToolCall": ("tool", "调用工具"),
    "dynamicToolCall": ("tool", "调用工具"),
    # Harness 原生工具面：这些名字此前不在白名单里，一律退化成
    # "使用工具处理任务"，用户看不出在做什么（2026-09-18 反馈）。
    "skill_read_manifest": ("read", "读取 Skill 清单"),
    "skill_read_instructions": ("read", "读取 Skill 说明"),
    "skill_read_resource": ("read", "读取 Skill 资源"),
    "mcp_list_tools": ("read", "查看 MCP 工具清单"),
    "mcp_read_tool_schema": ("read", "读取 MCP 工具说明"),
    "mcp_call_tool": ("tool", "调用 MCP 工具"),
    "search_knowledge": ("search", "检索知识库"),
    "search_knowledge_base": ("search", "检索知识库"),
    "knowledge_search": ("search", "检索知识库"),
    "artifact_read": ("read", "读取外置结果"),
    "sandbox_read_file": ("read", "查看沙箱文件"),
    "sandbox_run_command": ("command", "运行沙箱命令"),
    "search_workspace_files": ("read", "搜索工作区"),
    "write_workspace_files": ("write", "编辑并保存文件"),
    "multi_edit_workspace_file": ("write", "编辑并保存文件"),
    "delete_workspace_file": ("write", "删除文件"),
    "lint_workspace_file": ("read", "检查文件"),
    "workspace_status": ("read", "查看工作区状态"),
    "sandbox_status": ("read", "查看沙箱状态"),
    "check_command_policy": ("read", "检查命令策略"),
    "execute_skills": ("command", "执行 Skill"),
    "run_code": ("command", "运行代码"),
    "webSearch": ("search", "搜索资料"),
    "fileChange": ("write", "编辑并保存文件"),
}
# Codex canonical items keep their native kind only in source metadata; the
# projection reads the kind and timestamps, never item content (no tool
# arguments, no results, no reasoning text).
_CODEX_TOOL_ITEMS = frozenset(
    {"commandExecution", "mcpToolCall", "dynamicToolCall", "webSearch", "fileChange"}
)
_TOOL_GROUP_KEYS = frozenset({"search", "fetch", "read", "write", "command", "tool"})


def _is_tool_group_key(key: str) -> bool:
    return key.partition(":")[0] in _TOOL_GROUP_KEYS
_SECRET = re.compile(
    r"(?i)\b(?:[\w-]*(?:api[_-]?key|token|secret|password|authorization|cookie)[\w-]*)"
    r"\s*(?:[:=]\s*|\s+)(?:[\"'][^\"'\n]*[\"']|[^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_KEY = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|[A-Za-z0-9_-]{40,})\b")


def _retry_summary(detail: dict[str, Any]) -> str:
    def number(key: str) -> int | None:
        value = detail.get(key)
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        )

    attempt = number("next_attempt")
    previous_attempt = number("model_attempt")
    if attempt is None and previous_attempt is not None:
        attempt = previous_attempt + 1
    maximum = number("max_attempts")
    delay = number("delay_ms") or number("retry_delay_ms")
    wait = f"，{delay / 1000:g} 秒后" if delay else "，即将"
    if attempt is not None and maximum is not None:
        suffix = f"（第 {attempt}/{max(attempt, maximum)} 次）"
    else:
        suffix = f"（第 {attempt} 次）" if attempt is not None else ""
    # 与 harness 侧同一纪律：区分"上游繁忙"与"其它失败"，避免把我们的
    # 响应校验问题显示成网络繁忙。
    #
    # ⚠️ 但**不能把类别整个藏掉**（2026-09-18）：文案统一成"模型服务繁忙"后，
    # 连我们自己触发的超时（例如空闲进度超时）也显示成"上游繁忙"，于是没人能
    # 判断到底是网关的问题还是我们的问题——用户直接质问"服务器没那么容易繁忙
    # 吧"。这里保留可读措辞，同时**把原始类别带上**（timeout / rate_limit /
    # unavailable / transport / 其它），让重试原因一眼可辨。
    category = str(detail.get("failure_category") or "").strip().lower()
    transient = {"rate_limit", "timeout", "unavailable", "transport"}
    # 类别未知时保持原措辞（不改既有语义）；拿到类别才改口径并附上。
    headline = (
        "模型调用未成功"
        if category and category not in transient
        else "模型服务繁忙"
    )
    if category:
        headline = f"{headline}（{category}）"
    return f"{headline}{wait}自动重试{suffix}"


def _failover_summary(detail: dict[str, Any]) -> str:
    """Human-readable, auditable model switch notice without provider internals."""

    current = _safe_text(detail.get("model")) or "当前模型"
    target = _safe_text(detail.get("failover_model")) or "备用模型"
    category = str(detail.get("failure_category") or "").strip().lower()
    reason = {
        "timeout": "长时间没有返回进度",
        "rate_limit": "暂时触发限流",
        "unavailable": "暂时不可用",
        "transport": "连接暂时中断",
    }.get(category, "本次调用未成功")
    return f"{current}{reason}，已自动切换到 {target}"


def safe_activity_url(value: Any) -> str | None:
    """Keep public HTTP links, without credentials, fragments or query secrets."""
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or "." not in host
            or parsed.username
            or parsed.password
            or host.endswith((".localhost", ".local", ".internal", ".lan"))
            or host == "localhost"
        ):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        # Validate the port too; malformed netlocs must not survive rendering.
        port = parsed.port
        authority = host if port is None else f"{host}:{port}"
        return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))
    except ValueError:
        return None


def _safe_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    text = re.sub(
        r"https?://[^\s<>]+",
        lambda match: safe_activity_url(match.group()) or "[链接已隐藏]",
        text,
    )
    text = _SECRET.sub("[敏感信息已隐藏]", text)
    text = _BEARER.sub("Bearer [已隐藏]", text)
    return _KEY.sub("[已隐藏]", text)[:280]


def _safe_commentary_text(value: Any) -> str:
    """Sanitize public progress and drop empty streaming fragments.

    Older persisted runs can contain a punctuation-only trailing chunk even
    after the runtime-side filter is tightened.  The activity projection is
    intentionally defensive so both old and new sessions render cleanly.
    """
    text = _safe_text(value)
    return text if re.search(r"[A-Za-z0-9\u3400-\u9fff]", text) else ""


_ACTIVITY_CLOCKS = (("ts", "runtime"), ("observed_ts", "studio_observed"))


def _finite_seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _observed_timestamp(event: Any) -> float | None:
    """Studio's persisted receipt time, never a replacement for runtime time."""
    value = (
        event.get("created_at") if isinstance(event, dict) else getattr(event, "created_at", None)
    )
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime) or value.utcoffset() is None:
            return None
        return _finite_seconds(value.timestamp())
    except (ValueError, OverflowError, OSError):
        return None


def _record_activity_time(
    target: dict[str, Any], record: dict[str, Any], *, boundary: str | None = None
) -> None:
    for clock, _source in _ACTIVITY_CLOCKS:
        value = record.get(clock)
        if boundary == "begin":
            target.setdefault(f"{clock}_begin", value)
        elif boundary == "end":
            # Preserve an explicitly missing end, so a later owner's clock
            # cannot turn an incomplete runtime interval into a made-up one.
            target[f"{clock}_end"] = value
        if value is not None:
            previous = target.get(f"{clock}_last")
            target[f"{clock}_last"] = value if previous is None else max(value, previous)


def _activity_start(target: dict[str, Any]) -> float:
    for suffix in ("begin", "last"):
        for clock, _source in _ACTIVITY_CLOCKS:
            value = target.get(f"{clock}_{suffix}")
            if value is not None:
                return value
    return float("inf")


def _activity_duration(
    calls: list[dict[str, Any]],
    owner: dict[str, Any],
    *,
    running: bool,
    ended: bool,
    now: float,
) -> dict[str, Any]:
    # A group is a span, including time between its calls, rather than the
    # sum of possibly parallel work. Every boundary must use the same clock.
    for clock, source in _ACTIVITY_CLOCKS:
        begins, ends = [], []
        for call in calls:
            begin = call.get(f"{clock}_begin")
            end = call.get(f"{clock}_end")
            if f"{clock}_end" not in call:
                if running and call.get("status") == "running":
                    end = now
                elif ended:
                    end = owner.get(f"{clock}_last")
            if begin is None or end is None or end < begin:
                break
            begins.append(begin)
            ends.append(end)
        else:
            if begins:
                return {
                    "durationMs": int((max(ends) - min(begins)) * 1000),
                    "durationSource": source,
                }
    return {}


def _records(events: list[Any]) -> list[dict[str, Any]]:
    from ksadk.harness.tool_display import safe_tool_display
    records = []
    seen = set()
    for event in events:
        data = event.get("data", {}) if isinstance(event, dict) else event.data
        if not isinstance(data, dict):
            continue
        native = data.get("runtimeEvent") or data.get("runtime_event") or {}
        if not isinstance(native, dict):
            continue
        source = native.get("source") or {}
        source = source if isinstance(source, dict) else {}
        metadata = source.get("metadata") or {}
        metadata = metadata if isinstance(metadata, dict) else {}
        native_run = str(source.get("native_run_id") or native.get("run_id") or "")
        # The same rich event can produce started/updated/completed canonical
        # items. A retry must be shown once, not as three separate attempts.
        identity = str(
            source.get("native_event_id") or native.get("event_id") or f"row-{len(records)}"
        )
        if native.get("item_kind") == "tool_call" and metadata.get("engine_kind") == "native":
            # One committed parallel batch may project several calls from the
            # same native event. Keep their item identities distinct.
            identity = f"{identity}:{native.get('item_id') or ''}"
        seen_key = (native_run, identity)
        if seen_key in seen:
            continue
        base = {
            "run": native_run,
            "scope": native.get("scope_id") or "",
            "id": identity,
            "session": metadata.get("session_id"),
            "framework": str(source.get("framework") or ""),
            "ts": (
                None
                if metadata.get("timestamp_source") == "unavailable"
                else _finite_seconds(native.get("timestamp"))
            ),
            "observed_ts": _observed_timestamp(event),
        }
        if metadata.get("engine_kind") == "native" and native.get("event_type") in {
            "run.started", "run.completed", "run.failed", "run.canceled", "run.interrupted",
        }:
            records.append({**base, "type": "agent.started" if native["event_type"] == "run.started"
                            else "agent.completed", "details": {"status": native.get("status")}})
            seen.add(seen_key)
            continue
        parts = []
        for field in ("snapshot", "initial"):
            content = native.get(field)
            if isinstance(content, dict) and isinstance(content.get("parts"), list):
                parts.extend(content["parts"])
        if isinstance(native.get("update"), dict):
            parts.append(native["update"])
        if native.get("item_kind") in {"message", "reasoning"}:
            if (
                native.get("item_kind") == "message"
                and metadata.get("native_event_type") == "text.completed"
                and metadata.get("phase") == "commentary"
            ):
                for part in parts:
                    text = _safe_commentary_text(
                        part.get("text") if isinstance(part, dict) else None
                    )
                    if text:
                        records.append(
                            {
                                **base,
                                "type": "public.commentary",
                                "details": {"text": text},
                            }
                        )
                        seen.add(seen_key)
                        break
                continue
            if metadata.get("native_event_type") not in {"model.call.failed", "run.progress"}:
                continue
            for part in parts:
                text = part.get("text") if isinstance(part, dict) else None
                # This is the fixed public retry summary, never provider
                # reasoning, error bodies, payloads or arbitrary output text.
                if isinstance(text, str) and re.fullmatch(
                    r"模型服务繁忙(?:（[^\n]{0,120}）)?(?:，[\d.]+ 秒后)?"
                    r"自动重试（第 \d+/\d+ 次）",
                    text,
                ):
                    records.append(
                        {
                            **base,
                            "type": "provider.retry",
                            "details": {"text": _safe_text(text)},
                        }
                    )
                    seen.add(seen_key)
                    break
            continue
        if native.get("item_kind") in {"tool_call", "data"}:
            if metadata.get("engine_kind") == "native" and native.get("item_kind") == "tool_call":
                call_id, name = metadata.get("call_id"), metadata.get("tool_name")
                event_kind = native.get("event_type")
                if isinstance(call_id, str) and call_id and isinstance(name, str) and name:
                    if event_kind in {"item.started", "item.updated"}:
                        status = "started"
                    elif event_kind == "item.completed":
                        result_status = metadata.get("tool_status")
                        status = (
                            "succeeded"
                            if result_status == "ok"
                            else "failed"
                            if result_status == "error"
                            else "interrupted"
                        )
                    else:
                        status = None
                    if status is not None:
                        records.append(
                            {
                                **base,
                                "type": (
                                    "tool.call.begin"
                                    if event_kind == "item.started"
                                    else "tool.call.progress" if event_kind == "item.updated"
                                    else "tool.call.end"
                                ),
                                "details": {
                                    "name": name, "call_id": call_id, "status": status,
                                    "tool_display": safe_tool_display(metadata.get("tool_display")),
                                },
                            }
                        )
                        seen.add(seen_key)
                continue
            # Non-harness runtimes (Codex) publish execution facts as canonical
            # tool/data items; their native kind survives in source metadata.
            # Translate them into the same record vocabulary as Harness so the
            # projection below stays runtime-agnostic.
            native_kind = str(metadata.get("native_item_kind") or "")
            native_event = str(native.get("event_type") or "")
            native_item = str(source.get("native_item_id") or identity)
            if native_kind in _CODEX_TOOL_ITEMS:
                if native_event == "item.started":
                    status = "running"
                elif native_event == "item.completed":
                    status = "succeeded"
                elif native_event in {"item.failed", "item.updated"}:
                    status = "failed" if native_event == "item.failed" else None
                else:
                    status = None
                if status is not None:
                    records.append(
                        {
                            **base,
                            "type": "tool.call.begin" if status == "running" else "tool.call.end",
                            "details": {
                                "name": native_kind,
                                "call_id": native_item,
                                "status": status,
                            },
                        }
                    )
                    seen.add(seen_key)
                continue
            if native_kind == "contextCompaction":
                if native_event == "item.started":
                    records.append({**base, "type": "context.compaction.started", "details": {}})
                    seen.add(seen_key)
                elif native_event == "item.completed":
                    records.append({**base, "type": "context.compaction.completed", "details": {}})
                    seen.add(seen_key)
                continue
            continue
        if native.get("item_kind") != "status":
            continue
        for part in parts:
            data_part = part.get("data") if isinstance(part, dict) else None
            if metadata.get("engine_kind") == "native" and isinstance(data_part, dict):
                kind = data_part.get("native_event_kind")
                if kind == "model.retry_scheduled":
                    records.append({**base, "type": "provider.retry",
                                    "details": {"text": _retry_summary(data_part)}})
                    seen.add(seen_key)
                elif (kind in {"effect.prepared", "effect.received"}
                      and data_part.get("kind") == "model"):
                    purpose = data_part.get("model_purpose")
                    if purpose != "compaction":
                        failed = data_part.get("model_response_kind") in {
                            "provider_retryable_error", "provider_fatal_error", "empty_response",
                            "malformed_tool_call",
                        }
                        records.append({**base, "type": (
                                        "model.call.started" if kind == "effect.prepared"
                                        else "model.call.failed" if failed
                                        else "model.call.completed"),
                                        "details": {
                                                    "call_id": data_part.get("operation_key"),
                                                    "status": "started" if kind == "effect.prepared"
                                                    else "failed" if failed else "completed"}})
                        seen.add(seen_key)
                elif kind in {"context.compaction.started", "context.compaction.completed"}:
                    records.append({**base, "type": kind, "details": {}})
                    seen.add(seen_key)
                break
            if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                continue
            try:
                payload = json.loads(part["text"])
            except (ValueError, TypeError):
                continue
            if not isinstance(payload, dict) or not isinstance(payload.get("details"), dict):
                continue
            if payload.get("event") not in {
                "tool.call.begin",
                "tool.call.end",
                "tool.output.updated",
                "model.call.started",
                "model.call.completed",
                "model.call.failed",
                "agent.started",
                "agent.completed",
                "context.compaction.started",
                "context.compaction.completed",
                "run.progress",
                "text.completed",
                "sandbox.executed",
            }:
                continue
            event_type, detail = payload["event"], payload["details"]
            if event_type == "tool.output.updated":
                event_type = "tool.call.progress"
            if event_type == "text.completed":
                text = _safe_commentary_text(detail.get("text"))
                if metadata.get("phase") == "commentary" and text:
                    records.append(
                        {
                            **base,
                            "type": "public.commentary",
                            "details": {"text": text},
                        }
                    )
                    seen.add(seen_key)
                break
            if (event_type == "run.progress" and detail.get("kind") == "provider.retry") or (
                event_type == "model.call.failed" and detail.get("action") == "retry_same_model"
            ):
                event_type, detail = "provider.retry", {"text": _retry_summary(detail)}
            elif event_type == "model.call.failed" and detail.get("action") == "failover":
                event_type, detail = "provider.failover", {"text": _failover_summary(detail)}
            records.append(
                {
                    **base,
                    "type": event_type,
                    "details": detail,
                }
            )
            seen.add(seen_key)
            break
    return records
