"""Human-readable activity drilldown derived from durable execution evidence.

This projection never reads model reasoning or tool arguments/results. Old
records retain honest action counts; richer records may attach an explicitly
public action description. Neither path rewrites the canonical event log.
"""

from __future__ import annotations

import ipaddress
import json
import re
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
}
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
    delay = number("delay_ms") or number("retry_delay_ms")
    wait = f"，{delay / 1000:g} 秒后" if delay else "，即将"
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
        }
        parts = []
        for field in ("snapshot", "initial"):
            content = native.get(field)
            if isinstance(content, dict) and isinstance(content.get("parts"), list):
                parts.extend(content["parts"])
        if isinstance(native.get("update"), dict):
            parts.append(native["update"])
        if native.get("item_kind") in {"message", "reasoning"}:
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
            }:
                continue
            event_type, detail = payload["event"], payload["details"]
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
    result = {"runId": run.id, "status": status, "activities": []}
    if run.runtime_type != "harness":
        return result
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
            if detail.get("status"):
                next_status = _STATUS.get(str(detail["status"]).lower(), "unknown")
                if child.get("status") not in {"completed", "failed", "cancelled"}:
                    child["status"] = next_status
            elif detail.get("kind") == "delegation.route":
                child.setdefault("status", "running")
    owners: OrderedDict[str, dict[str, Any]] = OrderedDict()
    owners[""] = {"groups": OrderedDict()}

    def owner_of(record: dict[str, Any]) -> str:
        native_run = str(record["run"])
        if ":sub:" not in native_run:
            return ""
        # Scope hashes include the child's run ID, so parent_scope_id is not
        # comparable to the parent's scope. The native child run suffix is the
        # delegate call ID emitted by Harness and shared by lifecycle notices.
        return native_run.rsplit(":", 1)[-1]

    for record in records:
        detail, event_type = record["details"], record["type"]
        owner_id = owner_of(record)
        owner = owners.setdefault(owner_id, {"groups": OrderedDict()})
        if owner_id and event_type == "agent.started":
            owner["status"] = "running"
        if owner_id and event_type == "agent.completed":
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
        phase = {
            "model.call.started": ("model", "分析与整理", "running"),
            "model.call.completed": ("model", "分析与整理", "completed"),
            "model.call.failed": ("model", "分析与整理", "failed"),
            "context.compaction.started": ("context", "压缩上下文", "running"),
            "context.compaction.completed": ("context", "压缩上下文", "completed"),
        }.get(event_type)
        if phase:
            key, label, phase_status = phase
            group = owner["groups"].setdefault(key, {"label": label, "calls": OrderedDict()})
            active = owner.setdefault("active_phases", {})
            if phase_status == "running" or key not in active:
                active[key] = record["id"]
            call = group["calls"].setdefault(active[key], {"label": label})
            call["status"] = phase_status
            if phase_status != "running":
                active.pop(key, None)
            if key == "model" and phase_status in {"completed", "failed"}:
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
        if name == "delegate_task":
            continue
        key, label = _ACTIONS.get(name, ("tool", "使用工具处理任务"))
        group = owner["groups"].setdefault(key, {"label": label, "calls": OrderedDict()})
        call_id = f"{record['scope']}:{detail.get('call_id') or record['id']}"
        call = group["calls"].setdefault(call_id, {"status": "unknown", "label": label})
        call["status"] = _STATUS.get(str(detail.get("status")), "unknown")
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
            for index, (call_id, call) in enumerate(group["calls"].items(), 1):
                state = call["status"]
                owner_status = children.get(owner_id, {}).get("status") or owner.get("status")
                if state == "running" and (status in _TERMINAL or owner_status in _TERMINAL):
                    state = "unknown"
                statuses.append(state)
                description = call.get("text") or f"第 {index} 次{call['label']}"
                prefix = {
                    "running": "正在",
                    "completed": "已完成",
                    "failed": "未完成",
                    "cancelled": "已取消",
                    "unknown": "未确认完成",
                }[state]
                item = {"id": call_id, "text": f"{prefix}：{description}"}
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
            if key == "model" and statuses and statuses[-1] == "completed":
                # A recovered model phase may include an earlier failed
                # attempt. Preserve that detail but don't label the entire
                # phase failed after an observed successful retry.
                if "running" not in statuses and "unknown" not in statuses:
                    group_status = "completed"
            groups.append(
                {
                    "id": f"{run.id}:{owner_id or 'parent'}:{key}",
                    "kind": "step",
                    "label": group["label"],
                    "status": group_status,
                    "details": details,
                    "count": len(group["calls"]),
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
            child_status = "unknown"
        groups = render_groups(child_id, owner)
        summaries = (
            [] if groups else [{"id": f"{child_id}:no-detail", "text": "暂未记录可展示的操作步骤"}]
        )
        result["activities"].append(
            {
                "id": f"{run.id}:child:{child_id}",
                "kind": "subagent",
                "label": metadata.get("label") or "子智能体",
                "status": child_status,
                "details": summaries,
                "children": groups,
            }
        )
    return result


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
