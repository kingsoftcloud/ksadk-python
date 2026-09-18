"""Human-readable activity drilldown derived from durable execution evidence.

This projection never reads model reasoning or tool arguments/results. Old
records retain honest action counts; richer records may attach an explicitly
public action description. Neither path rewrites the canonical event log.
"""

from __future__ import annotations

import ipaddress
import json
import re
import time
from collections import OrderedDict
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
    return f"模型服务繁忙{wait}自动重试{suffix}"


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


def _records(events: list[Any]) -> list[dict[str, Any]]:
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
        seen_key = (native_run, identity)
        if seen_key in seen:
            continue
        base = {
            "run": native_run,
            "scope": native.get("scope_id") or "",
            "id": identity,
            "session": metadata.get("session_id"),
            "framework": str(source.get("framework") or ""),
            "ts": native.get("timestamp")
            if isinstance(native.get("timestamp"), (int, float))
            else None,
        }
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
                "model.call.started",
                "model.call.completed",
                "model.call.failed",
                "agent.started",
                "agent.completed",
                "context.compaction.started",
                "context.compaction.completed",
                "run.progress",
                "text.completed",
            }:
                continue
            event_type, detail = payload["event"], payload["details"]
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


def project_run_activities(run: Any, events: list[Any]) -> dict[str, Any]:
    status = str(getattr(run.status, "value", run.status)).lower()
    observed_at = time.time()
    result = {"runId": run.id, "status": status, "activities": []}
    records = _records(events)
    handle = getattr(run, "runtime_handle", {})
    handle = handle if isinstance(handle, dict) else {}
    own_runs = {
        value for value in (run.id, handle.get("run_id")) if isinstance(value, str) and value
    }
    own_session = getattr(run, "session_id", None)
    records = [
        record
        for record in records
        if (
            not record["run"]
            # Provider-native run ids (e.g. Codex turn ids) are not Studio run
            # identities; the per-run event fetch already scopes their events.
            or record.get("framework") not in (None, "", "harness")
            or any(
                record["run"] == root or record["run"].startswith(f"{root}:sub:")
                for root in own_runs
            )
        )
        and (not own_session or not record["session"] or record["session"] == own_session)
    ]
    children: dict[str, dict[str, Any]] = {}
    # Lifecycle notices can arrive after a child's tools. Resolve labels first.
    for record in records:
        detail = record["details"]
        if record["type"] == "run.progress" and detail.get("kind") in {
            "subagent.event",
            "delegation.route",
        }:
            call_id = str(detail.get("call_id") or "")
            if not call_id:
                continue
            child = children.setdefault(call_id, {})
            label = _safe_text(detail.get("label"))
            if label:
                child["label"] = label
            if record.get("ts") is not None:
                if child.get("ts_begin") is None:
                    child["ts_begin"] = record["ts"]
                child["ts_last"] = record["ts"]
            provider_ref = str(detail.get("provider_ref") or "")
            if provider_ref:
                child["provider"] = "Codex" if "codex" in provider_ref else "Harness"
            if detail.get("status"):
                next_status = _STATUS.get(str(detail["status"]).lower(), "unknown")
                if child.get("status") not in {"completed", "failed", "cancelled"}:
                    child["status"] = next_status
                    if next_status in {"completed", "failed", "cancelled"}:
                        child["ts_end"] = record.get("ts")
            elif detail.get("kind") == "delegation.route":
                child.setdefault("status", "running")
            summary = _safe_text(detail.get("public_summary"))
            if summary:
                child["summary"] = summary
                child["summary_ts"] = record.get("ts")
    owners: OrderedDict[str, dict[str, Any]] = OrderedDict()
    owners[""] = {"groups": OrderedDict()}

    def owner_of(record: dict[str, Any]) -> str:
        return owner_of_record_run(record)

    for record in records:
        detail, event_type = record["details"], record["type"]
        owner_id = owner_of(record)
        owner = owners.setdefault(owner_id, {"groups": OrderedDict()})
        if record.get("ts") is not None:
            owner["ts_last"] = max(record["ts"], owner.get("ts_last") or record["ts"])
        if event_type == "agent.started":
            owner["status"] = "running"
        if event_type == "agent.completed":
            owner["status"] = _STATUS.get(str(detail.get("status")), "unknown")
        if event_type == "provider.retry":
            group = owner["groups"].setdefault(
                "retry", {"label": "自动重试", "calls": OrderedDict()}
            )
            group["calls"][record["id"]] = {
                "status": "running",
                "label": "自动重试",
                "text": detail["text"],
                "retry": True,
            }
            # The event proves the current model attempt failed; keep that
            # evidence instead of leaving its started record running forever.
            active_model = owner.setdefault("active_phases", {}).pop("model", None)
            model_group = owner["groups"].get("model")
            if active_model and model_group:
                model_group["calls"][active_model]["status"] = "failed"
            continue
        if event_type == "public.commentary":
            commentary = {
                "id": f"{run.id}:commentary:{record['id']}",
                "kind": "commentary",
                "label": "公开进展",
                "status": "completed",
                "text": detail["text"],
                "details": [],
                "_sortTs": (
                    record["ts"]
                    if isinstance(record.get("ts"), (int, float))
                    else float("inf")
                ),
            }
            if owner_id:
                # Child progress belongs behind that child's Details affordance.
                # Publishing every child checkpoint in the parent timeline turns
                # a three-worker task into a wall of status text and makes it
                # indistinguishable from the parent's own stage updates.
                owner.setdefault("commentary", []).append(commentary)
            else:
                result["activities"].append(commentary)
            continue
        phase = {
            "model.call.started": ("model", "running"),
            "model.call.completed": ("model", "completed"),
            "model.call.failed": ("model", "failed"),
            "context.compaction.started": ("context", "running"),
            "context.compaction.completed": ("context", "completed"),
        }.get(event_type)
        if phase:
            base_key, phase_status = phase
            # Deterministic phase titles derived from observed execution
            # order: a model phase before any tool work is task analysis;
            # after tool work it is result synthesis. The model never
            # invents its own status labels.
            if base_key == "model" and (
                owner.get("tool_seen")
                or any(group_key in owner["groups"] for group_key in _TOOL_GROUP_KEYS)
            ):
                key, label = "model:summary", "汇总结果"
            else:
                key = base_key
                label = "分析任务" if base_key == "model" else "压缩上下文"
            group = owner["groups"].setdefault(key, {"label": label, "calls": OrderedDict()})
            active = owner.setdefault("active_phases", {})
            if phase_status == "running" or key not in active:
                active[key] = record["id"]
            call = group["calls"].setdefault(active[key], {"label": label})
            call["status"] = phase_status
            # These are deterministic execution summaries, not model chain of
            # thought.  They give the expandable row a useful human-facing
            # explanation without exposing prompts, parameters or outputs.
            call["text"] = {
                "model": "理解任务并确定执行步骤",
                "model:summary": "整理已完成的执行结果",
                "context": "整理并压缩较早的上下文，关键执行状态继续保留",
            }[key]
            if record.get("ts") is not None:
                if phase_status == "running":
                    call["ts_begin"] = record["ts"]
                else:
                    call["ts_end"] = record["ts"]
                    call.setdefault("ts_begin", record["ts"])
            if phase_status != "running":
                active.pop(key, None)
            if base_key == "model" and phase_status in {"completed", "failed"}:
                for retry in owner["groups"].get("retry", {}).get("calls", {}).values():
                    if retry["status"] == "running":
                        retry["status"] = phase_status
                        retry["outcome"] = (
                            "重试后已恢复" if phase_status == "completed" else "重试后仍未成功"
                        )
            continue
        if event_type not in {"tool.call.begin", "tool.call.end"}:
            continue
        name = str(detail.get("name") or "tool")
        # Delegation is rendered as a sub-agent row instead of a generic tool
        # row, but it still separates the analysis model turn from the later
        # result-synthesis turn.
        owner["tool_seen"] = True
        if name == "delegate_task":
            continue
        key, label = _ACTIONS.get(name, ("tool", "使用工具处理任务"))
        group = owner["groups"].setdefault(key, {"label": label, "calls": OrderedDict()})
        call_id = f"{record['scope']}:{detail.get('call_id') or record['id']}"
        call = group["calls"].setdefault(call_id, {"status": "unknown", "label": label})
        call["status"] = _STATUS.get(str(detail.get("status")), "unknown")
        if record.get("ts") is not None:
            if record["type"] == "tool.call.begin":
                call.setdefault("ts_begin", record["ts"])
            else:
                call["ts_end"] = record["ts"]
                call.setdefault("ts_begin", record["ts"])
        action = detail.get("public_action")
        if isinstance(action, dict):
            text = _safe_text(action.get("text"))
            if text:
                call["text"] = text
            href = safe_activity_url(action.get("href"))
            if href:
                call["href"] = href

    def render_groups(owner_id: str, owner: dict[str, Any]) -> list[dict[str, Any]]:
        groups = []
        for key, group in owner["groups"].items():
            details = []
            statuses = []
            generic: OrderedDict[tuple[str, str], list[dict[str, str]]] = OrderedDict()
            # A retried public action is often recorded as two independent
            # tool calls.  Keep the failed attempt in the drilldown, but do
            # not let it poison the whole group once a later call for the
            # exact same public target succeeds.  Calls without a public
            # target remain conservative: their failure cannot be inferred
            # away from labels or private arguments/results.
            recovered_failures: set[str] = set()
            completed_targets: set[tuple[str, str]] = set()
            for call_id, call in reversed(group["calls"].items()):
                target = (
                    ("href", call["href"])
                    if call.get("href")
                    else (("text", call["text"]) if call.get("text") else None)
                )
                if key in _TOOL_GROUP_KEYS and call["status"] == "completed" and target:
                    completed_targets.add(target)
                elif (
                    key in _TOOL_GROUP_KEYS
                    and call["status"] == "failed"
                    and target in completed_targets
                ):
                    recovered_failures.add(call_id)
            calls_in_order = list(group["calls"].items())
            if (
                key == "write"
                and calls_in_order
                and calls_in_order[-1][1]["status"] == "completed"
            ):
                # A malformed/truncated model tool call can fail before its
                # arguments are safe enough to expose.  If this same document
                # delivery phase ends with an observed successful write, the
                # earlier anonymous write failures are retry history, not the
                # outcome of the phase.  Keep them in the drilldown but do not
                # paint the successfully delivered artifact red.
                recovered_failures.update(
                    call_id
                    for call_id, call in calls_in_order[:-1]
                    if call["status"] == "failed" and not call.get("text")
                )
            for index, (call_id, call) in enumerate(group["calls"].items(), 1):
                state = call["status"]
                owner_status = children.get(owner_id, {}).get("status") or owner.get("status")
                if state == "running" and (status in _TERMINAL or owner_status in _TERMINAL):
                    # The provider may omit a final model.call.completed after
                    # an approval resume. A canonical agent.completed event is
                    # positive evidence that the still-open model phase did
                    # finish; it is not enough to invent completion for tools.
                    if key.startswith("model") and owner_status == "completed":
                        state = "completed"
                    else:
                        state = (
                            "cancelled"
                            if status == "cancelled" or owner_status == "cancelled"
                            else "unknown"
                        )
                effective_state = "completed" if call_id in recovered_failures else state
                statuses.append(effective_state)
                description = call.get("text") or f"第 {index} 次{call['label']}"
                prefix = {
                    "running": "正在",
                    "completed": "已完成",
                    "failed": "未完成",
                    "cancelled": "已取消",
                    "unknown": "未确认完成",
                }[state]
                item = {"id": call_id, "text": f"{prefix}：{description}"}
                if call_id in recovered_failures:
                    item["text"] = f"已重试：{description}；后续已成功"
                if call.get("retry"):
                    item["text"] = description
                    if call.get("outcome"):
                        item["text"] += f"；{call['outcome']}"
                    elif state == "unknown":
                        item["text"] += "；未记录重试结果"
                if call.get("href"):
                    item["href"] = call["href"]
                details.append(item)
                if not call.get("text"):
                    generic.setdefault((state, call["label"]), []).append(item)
            # Older events have only action names. Forty-seven identical
            # numbered rows add no evidence; summarize their real count while
            # keeping failed/cancelled/unfinished calls separately visible.
            for (state, label), items in generic.items():
                if len(items) < 4:
                    continue
                first = items[0]
                prefix = {
                    "running": "正在执行",
                    "completed": "已完成",
                    "failed": "未完成",
                    "cancelled": "已取消",
                    "unknown": "未确认完成",
                }[state]
                first["text"] = f"{prefix} {len(items)} 次{label}（历史记录未保存更细的操作描述）"
                other_ids = {item["id"] for item in items[1:]}
                details = [item for item in details if item["id"] not in other_ids]
            group_status = next(
                (
                    value
                    for value in ("running", "failed", "unknown", "cancelled")
                    if value in statuses
                ),
                "completed",
            )
            owner_status = children.get(owner_id, {}).get("status") or owner.get("status")
            if group_status == "failed" and owner_status in {"running", "completed"}:
                # A failed search/fetch attempt is not the outcome of a child
                # that is still working or has ultimately completed. Keep the
                # failed attempts visible in drilldown, but present the group
                # as partial evidence rather than declaring the whole
                # sub-agent execution failed.
                group_status = "partial"
            if key == "model" and statuses and statuses[-1] == "completed":
                # A recovered model phase may include an earlier failed
                # attempt. Preserve that detail but don't label the entire
                # phase failed after an observed successful retry.
                if "running" not in statuses and "unknown" not in statuses:
                    group_status = "completed"
            if key in {"model", "model:summary", "context"} and details:
                # Internal model turns are implementation detail.  A phase
                # may contain many calls, but users need one truthful phase
                # summary rather than seven identical rows and a "7 次"
                # counter. Retry evidence remains separately visible in the
                # dedicated retry activity.
                description = next(
                    (
                        call.get("text")
                        for call in reversed(group["calls"].values())
                        if call.get("text")
                    ),
                    group["label"],
                )
                prefix = {
                    "running": "正在",
                    "completed": "已完成",
                    "failed": "未完成",
                    "cancelled": "已取消",
                    "unknown": "未确认完成",
                }[group_status]
                details = [
                    {
                        "id": f"{run.id}:{owner_id or 'parent'}:{key}:summary",
                        "text": f"{prefix}：{description}",
                    }
                ]
            begins = [
                call["ts_begin"]
                for call in group["calls"].values()
                if isinstance(call.get("ts_begin"), (int, float))
            ]
            ends = [
                call["ts_end"]
                for call in group["calls"].values()
                if isinstance(call.get("ts_end"), (int, float))
            ]
            duration_ms = None
            if begins:
                owner_status = children.get(owner_id, {}).get("status") or owner.get("status")
                still_running = (
                    "running" in statuses
                    and status not in _TERMINAL
                    and owner_status not in _TERMINAL
                )
                latest_end = (
                    observed_at if still_running else (max(ends) if ends else owner.get("ts_last"))
                )
                if isinstance(latest_end, (int, float)) and latest_end >= min(begins):
                    duration_ms = int((latest_end - min(begins)) * 1000)
            groups.append(
                {
                    "id": f"{run.id}:{owner_id or 'parent'}:{key}",
                    "kind": "step",
                    "label": group["label"],
                    "status": group_status,
                    "details": details,
                    **(
                        {}
                        if key in {"model", "model:summary", "context"}
                        else {"count": len(group["calls"])}
                    ),
                    **({"durationMs": duration_ms} if duration_ms is not None else {}),
                    "_sortTs": min(begins) if begins else float("inf"),
                }
            )
        return groups

    result["activities"].extend(render_groups("", owners[""]))
    # A lifecycle-only child must remain visible even without tool records.
    for child_id in children:
        owners.setdefault(child_id, {"groups": OrderedDict()})
    for child_id, owner in owners.items():
        if not child_id:
            continue
        metadata = children.get(child_id, {})
        child_status = metadata.get("status") or owner.get("status") or "unknown"
        if child_status == "running" and status in _TERMINAL:
            child_status = "cancelled" if status == "cancelled" else "unknown"
        groups = render_groups(child_id, owner)
        summaries = (
            [] if groups else [{"id": f"{child_id}:no-detail", "text": "暂未记录可展示的操作步骤"}]
        )
        duration_ms = None
        ts_begin, ts_end = metadata.get("ts_begin"), metadata.get("ts_end")
        if isinstance(ts_begin, (int, float)):
            latest = (
                observed_at
                if child_status == "running" and status not in _TERMINAL
                else ts_end
                if isinstance(ts_end, (int, float))
                else owner.get("ts_last")
            )
            if isinstance(latest, (int, float)) and latest >= ts_begin:
                duration_ms = int((latest - ts_begin) * 1000)
        entry: dict[str, Any] = {
            "id": f"{run.id}:child:{child_id}",
            "kind": "subagent",
            "label": metadata.get("label") or "子智能体",
            "status": child_status,
            "details": summaries,
            "children": groups,
            "callId": child_id,
            "_sortTs": (
                ts_begin
                if isinstance(ts_begin, (int, float))
                else owner.get("ts_last", float("inf"))
            ),
        }
        if duration_ms is not None:
            entry["durationMs"] = duration_ms
        if metadata.get("provider"):
            entry["provider"] = metadata["provider"]
        result["activities"].append(entry)
    # Parent phases, tools and delegated work are projected in their observed
    # execution order.  This lets a live timeline naturally read as
    # analysis → sub-agent/tool work → optional compaction → result synthesis,
    # rather than putting every child after all parent phases.
    result["activities"].sort(key=lambda activity: activity.get("_sortTs", float("inf")))
    for activity in result["activities"]:
        activity.pop("_sortTs", None)
    return result


def _subagent_detail(run: Any, events: list[Any], call_id: str) -> dict[str, Any]:
    """Curated sub-agent facts for the detail panel.

    Same evidence rules as the parent projection: lifecycle metadata and
    public action descriptions only — never tool arguments, results or
    model reasoning.
    """

    projection = project_run_activities(run, events)
    entry = next(
        (
            activity
            for activity in projection["activities"]
            if activity.get("kind") == "subagent" and activity.get("callId") == call_id
        ),
        None,
    )
    if entry is None:
        return {}
    models = []
    own_session = getattr(run, "session_id", None)
    child_sessions = []
    for record in _records(events):
        if owner_of_record_run(record) != call_id:
            continue
        model = record["details"].get("model") if record["type"].startswith("model.call") else None
        if model:
            model = _safe_text(model)
            if model and model not in models:
                models.append(model)
        if (
            record.get("session")
            and own_session
            and record["session"] != own_session
            and record["session"] not in child_sessions
        ):
            child_sessions.append(record["session"])
    child_progress = []
    for record in _records(events):
        if owner_of_record_run(record) != call_id or record["type"] != "public.commentary":
            continue
        child_progress.append(
            {
                "id": f"{run.id}:child:{call_id}:progress:{record['id']}",
                "kind": "commentary",
                "label": "阶段进展",
                "status": "completed",
                "text": record["details"]["text"],
                "details": [],
            }
        )
    # Keep the detail panel useful without replaying the child's entire chat.
    # The first checkpoint explains the approach and the last two show the
    # latest work; tool groups below retain the concrete execution evidence.
    if len(child_progress) > 3:
        child_progress = [child_progress[0], *child_progress[-2:]]
    execution_facts = entry.get("children") or ([] if child_progress else entry["details"])
    detail = {
        "runId": run.id,
        "callId": call_id,
        "label": entry["label"],
        "status": entry["status"],
        "facts": [*child_progress, *execution_facts],
        "parentStatus": projection["status"],
    }
    for key in ("durationMs", "provider"):
        if entry.get(key) is not None:
            detail[key] = entry[key]
    if models:
        detail["models"] = models
    if child_sessions:
        detail["sessionId"] = child_sessions[0]
    return detail


def owner_of_record_run(record: dict[str, Any]) -> str:
    # Scope hashes include the child's run ID, so parent_scope_id is not
    # comparable to the parent's scope. The native child run suffix is the
    # delegate call ID emitted by Harness and shared by lifecycle notices.
    native_run = str(record["run"])
    if ":sub:" not in native_run:
        return ""
    return native_run.rsplit(":", 1)[-1]


def register_activity_routes(app: Any, studio: Any, *, resolve_run_id: Any = None) -> None:
    """Use the Studio app's existing session/CSRF security middleware."""

    @app.get("/api/v1/runs/{run_id}/activities")
    async def run_activities(run_id: str):
        from fastapi.responses import JSONResponse

        resolved_id = resolve_run_id(run_id) if resolve_run_id else run_id
        resolve_owner = getattr(studio, "runtime_for_run", None)
        owner = resolve_owner(resolved_id) if callable(resolve_owner) else studio
        if owner is None:
            from ksadk.studio.errors import not_found

            raise not_found("run", resolved_id)
        run = owner.event_store.get(resolved_id)
        events = await owner.run_service.events(resolved_id)
        return JSONResponse(
            project_run_activities(run, events), headers={"Cache-Control": "no-store"}
        )

    @app.get("/api/v1/runs/{run_id}/subagents/{call_id}")
    async def run_subagent(run_id: str, call_id: str):
        from fastapi.responses import JSONResponse

        from ksadk.studio.errors import not_found

        resolved_id = resolve_run_id(run_id) if resolve_run_id else run_id
        resolve_owner = getattr(studio, "runtime_for_run", None)
        owner = resolve_owner(resolved_id) if callable(resolve_owner) else studio
        if owner is None:
            error = not_found("run", resolved_id)
        else:
            run = owner.event_store.get(resolved_id)
            events = await owner.run_service.events(resolved_id)
            detail = _subagent_detail(run, events, call_id)
            if detail:
                return JSONResponse(detail, headers={"Cache-Control": "no-store"})
            error = not_found("subagent", call_id)
        return JSONResponse(
            {
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "details": error.details or {},
                }
            },
            status_code=error.status_code,
            headers={"Cache-Control": "no-store"},
        )
