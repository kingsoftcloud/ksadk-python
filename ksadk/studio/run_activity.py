"""Human-readable activity projection and local HTTP routes."""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from typing import Any

from ksadk.studio.activity_records import (
    _ACTIONS as _ACTIONS,
)
from ksadk.studio.activity_records import (
    _ACTIVITY_CLOCKS as _ACTIVITY_CLOCKS,
)
from ksadk.studio.activity_records import (
    _BEARER as _BEARER,
)
from ksadk.studio.activity_records import (
    _CODEX_TOOL_ITEMS as _CODEX_TOOL_ITEMS,
)
from ksadk.studio.activity_records import (
    _KEY as _KEY,
)
from ksadk.studio.activity_records import (
    _SECRET as _SECRET,
)
from ksadk.studio.activity_records import (
    _STATUS as _STATUS,
)
from ksadk.studio.activity_records import (
    _TERMINAL as _TERMINAL,
)
from ksadk.studio.activity_records import (
    _TOOL_GROUP_KEYS as _TOOL_GROUP_KEYS,
)
from ksadk.studio.activity_records import (
    _activity_duration as _activity_duration,
)
from ksadk.studio.activity_records import (
    _activity_start as _activity_start,
)
from ksadk.studio.activity_records import (
    _failover_summary as _failover_summary,
)
from ksadk.studio.activity_records import (
    _finite_seconds as _finite_seconds,
)
from ksadk.studio.activity_records import (
    _is_tool_group_key as _is_tool_group_key,
)
from ksadk.studio.activity_records import (
    _observed_timestamp as _observed_timestamp,
)
from ksadk.studio.activity_records import (
    _record_activity_time as _record_activity_time,
)
from ksadk.studio.activity_records import (
    _records as _records,
)
from ksadk.studio.activity_records import (
    _retry_summary as _retry_summary,
)
from ksadk.studio.activity_records import (
    _safe_commentary_text as _safe_commentary_text,
)
from ksadk.studio.activity_records import (
    _safe_text as _safe_text,
)
from ksadk.studio.activity_records import (
    safe_activity_url as safe_activity_url,
)


def project_run_activities(run: Any, events: list[Any]) -> dict[str, Any]:
    status = str(getattr(run.status, "value", run.status)).lower()
    observed_at = time.time()
    result = {"runId": run.id, "status": status, "activities": []}
    from ksadk.studio.shared_web_response_events import _APPROVAL_DECISIONS

    approvals = {}
    for event in events:
        event_type = event.get("type") if isinstance(event, dict) else event.type
        data = event.get("data", {}) if isinstance(event, dict) else event.data
        if event_type != "approval.resolved" or not isinstance(data, dict):
            continue
        native_response = (data.get("runtimeEvent") or {}).get("response") or {}
        raw = str(data.get("decision") or native_response.get("decision")
                  or data.get("outcome") or data.get("name") or "").strip().lower()
        approval_id = str(data.get("approvalId")
                          or (data.get("runtimeEvent") or {}).get("interaction_id") or "unknown")
        approvals[approval_id] = _APPROVAL_DECISIONS.get(raw, "unknown")
    result["approvalAlerts"] = [
        {"id": identity, "status": decision}
        for identity, decision in approvals.items() if decision == "unknown"
    ]
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
            _record_activity_time(child, record, boundary="begin")
            provider_ref = str(detail.get("provider_ref") or "")
            if provider_ref:
                child["provider"] = "Codex" if "codex" in provider_ref else "Harness"
            if detail.get("status"):
                next_status = _STATUS.get(str(detail["status"]).lower(), "unknown")
                if child.get("status") not in {"completed", "failed", "cancelled"}:
                    child["status"] = next_status
                    if next_status in {"completed", "failed", "cancelled"}:
                        _record_activity_time(child, record, boundary="end")
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
        _record_activity_time(owner, record)
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
        if event_type == "provider.failover":
            group = owner["groups"].setdefault(
                "model.failover", {"label": "已切换模型", "calls": OrderedDict()}
            )
            group["calls"][record["id"]] = {
                "status": "completed",
                "label": "已切换模型",
                "text": detail["text"],
            }
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
                    record["ts"] if record["ts"] is not None else record["observed_ts"]
                    if record["observed_ts"] is not None
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
        if event_type == "sandbox.executed":
            # G7 UI ③：沙箱执行投影成 activity（会话页沙箱徽标消费）
            backend_id = str(detail.get("backend_id") or "unknown")
            rejected = bool(detail.get("rejected"))
            sandbox_act = {
                "id": f"{run.id}:sandbox:{record['id']}",
                "kind": "sandbox",
                "label": f"沙箱执行 · {backend_id}",
                "status": "failed" if rejected else "completed",
                "backend_id": backend_id,
                "capabilities": detail.get("capabilities") or {},
                "rejected": rejected,
                "details": [],
                "_sortTs": (
                    record["ts"] if record["ts"] is not None else record["observed_ts"]
                    if record["observed_ts"] is not None
                    else float("inf")
                ),
            }
            if owner_id:
                owner.setdefault("sandbox", []).append(sandbox_act)
            else:
                result["activities"].append(sandbox_act)
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
            # 模型回合一律显示为"思考"——对齐 Claude Code 的展示标准：
            # 它把工具调用显示成事实单行（`⏺ Bash(npm test)` / "Searched for 13
            # patterns, read 6 files"），而模型回合就是 thinking，**不发明
            # "分析任务 / 汇总结果 / 整理已完成的执行结果"这类语义标签**。
            # 2026-09-18 实测：把"工具之后的模型回合"命名成"整理已完成的执行结果"，
            # 会让流程看起来"一直在汇总却零产出"，用户据此判断流程不对——标签本身
            # 在制造误解。工具步骤仍保留真实工具名（那是事实）。
            if base_key == "model" and (
                owner.get("tool_seen")
                or any(_is_tool_group_key(group_key) for group_key in owner["groups"])
            ):
                key, label = "model:summary", "思考"
            else:
                key = base_key
                label = "思考" if base_key == "model" else "压缩上下文"
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
                "model": "模型正在思考",
                "model:summary": "模型正在思考下一步（此前已有工具执行）",
                "context": "整理并压缩较早的上下文，关键执行状态继续保留",
            }[key]
            _record_activity_time(
                call, record, boundary="begin" if phase_status == "running" else "end"
            )
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
        if event_type not in {"tool.call.begin", "tool.call.end", "tool.call.progress"}:
            continue
        name = str(detail.get("name") or "tool")
        # Delegation is rendered as a sub-agent row instead of a generic tool
        # row, but it still separates the analysis model turn from the later
        # result-synthesis turn.
        owner["tool_seen"] = True
        if name == "delegate_task":
            continue
        base_key, label = _ACTIONS.get(name, ("tool", "使用工具处理任务"))
        # Do not merge every read-like action under the first label observed.
        # A run that reads a Skill, checks the workspace and validates files
        # used to become one misleading “读取 Skill 清单 22 次” row.  Group by
        # human-facing action while retaining the broad category for outcome
        # and recovery rules below.
        key = f"{base_key}:{label}"
        group = owner["groups"].setdefault(key, {"label": label, "calls": OrderedDict()})
        call_id = f"{record['scope']}:{detail.get('call_id') or record['id']}"
        call = group["calls"].setdefault(call_id, {
            "status": "unknown", "label": label,
            "callId": str(detail.get("call_id") or record["id"]),
        })
        call["status"] = _STATUS.get(str(detail.get("status")), "unknown")
        _record_activity_time(
            call, record, boundary=("begin" if record["type"] == "tool.call.begin"
                                    else "end" if record["type"] == "tool.call.end" else None)
        )
        action = detail.get("public_action")
        from ksadk.harness.tool_display import safe_tool_display

        display = safe_tool_display(detail.get("tool_display"))
        if display:
            # A completion-only managed event may have no original arguments;
            # preserve start metadata rather than replacing it with an empty view.
            previous_display = call.get("display", {})
            call["display"] = {**previous_display, **{k: v for k, v in display.items()
                                                       if v not in ("", None)}}
            if (event_type == "tool.call.end" and display.get("category") == "terminal"
                    and type(display.get("exit_code")) is int and display["exit_code"] != 0):
                call["status"] = "failed"
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
                if _is_tool_group_key(key) and call["status"] == "completed" and target:
                    completed_targets.add(target)
                elif (
                    _is_tool_group_key(key)
                    and call["status"] == "failed"
                    and target in completed_targets
                ):
                    recovered_failures.add(call_id)
            calls_in_order = list(group["calls"].items())
            if (
                key.partition(":")[0] == "write"
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
                    "partial": "部分完成",
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
            calls = list(group["calls"].values())
            owner_status = children.get(owner_id, {}).get("status") or owner.get("status")
            duration = _activity_duration(
                calls,
                owner,
                running=(
                    "running" in statuses
                    and status not in _TERMINAL
                    and owner_status not in _TERMINAL
                ),
                ended=status in _TERMINAL or owner_status in _TERMINAL,
                now=observed_at,
            )
            groups.append(
                {
                    "id": f"{run.id}:{owner_id or 'parent'}:{key}",
                    "kind": "step",
                    "category": (
                        "reasoning"
                        if key in {"model", "model:summary"}
                        else "context"
                        if key == "context"
                        else "recovery"
                        if key in {"retry", "model.failover"}
                        else "tool"
                    ),
                    "label": group["label"],
                    "status": group_status,
                    "details": details,
                    "tools": [
                        {"id": call_id, "callId": call.get("callId", call_id),
                         "status": ("cancelled" if status == "cancelled" else "unknown")
                         if call["status"] == "running" and (
                             status in _TERMINAL or owner_status in _TERMINAL
                         ) else call["status"],
                         "display": call["display"]}
                        for call_id, call in group["calls"].items() if call.get("display")
                    ],
                    **(
                        {}
                        if key in {"model", "model:summary", "context"}
                        else {"count": len(group["calls"])}
                    ),
                    **duration,
                    "_sortTs": min((_activity_start(call) for call in calls), default=float("inf")),
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
        # ``_sortTs`` is an internal ordering sentinel and may be ``inf`` when
        # legacy child events have no timestamp. Nested groups are not part of
        # the top-level cleanup loop below, so remove it before JSON encoding.
        for group in groups:
            group.pop("_sortTs", None)
        summaries = (
            [] if groups else [{"id": f"{child_id}:no-detail", "text": "暂未记录可展示的操作步骤"}]
        )
        duration = _activity_duration(
            [metadata],
            owner,
            running=child_status == "running" and status not in _TERMINAL,
            ended=status in _TERMINAL or child_status in _TERMINAL,
            now=observed_at,
        )
        entry: dict[str, Any] = {
            "id": f"{run.id}:child:{child_id}",
            "kind": "subagent",
            "label": metadata.get("label") or "子智能体",
            "status": child_status,
            "details": summaries,
            "children": groups,
            "callId": child_id,
            **duration,
            "_sortTs": _activity_start(metadata),
        }
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
    for key in ("durationMs", "durationSource", "provider"):
        if entry.get(key) is not None:
            detail[key] = entry[key]
    if models:
        detail["models"] = models
    if child_sessions:
        detail["sessionId"] = child_sessions[0]
    detail["messages"] = _public_child_messages(
        events, lambda native_run: native_run.endswith(f":sub:{call_id}")
    )
    return detail


def _public_child_messages(events: list[Any], matches: Any) -> list[dict]:
    from ksadk.harness.public_activity import public_commentary_text

    # Explicit public assistant message channel, never reasoning/status/tool
    # snapshots. Ordered item identity handles append, replace and retirement.
    messages = OrderedDict()
    for event in events:
        data = event.get("data", {}) if isinstance(event, dict) else event.data
        native = (data.get("runtimeEvent") or {}) if isinstance(data, dict) else {}
        source = native.get("source") or {}
        native_run = source.get("native_run_id") or native.get("run_id") or ""
        if not matches(str(native_run)) or native.get("item_kind") != "message":
            continue
        identity = native.get("item_id")
        if not identity:
            continue
        snapshot = native.get("snapshot") or native.get("initial") or {}
        parts = snapshot.get("parts", [])
        update = native.get("update") or {}
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict)
                       and isinstance(part.get("text", ""), str))
        if native.get("event_type") == "item.updated":
            delta = update.get("text", "")
            if isinstance(delta, str):
                previous_text = (messages.get(identity, {}).get("text", "")
                                 if native.get("op") == "append" else "")
                text = previous_text + delta
        if text:
            safe = public_commentary_text(text, limit=3000)
            if safe:
                messages[identity] = {"id": identity, "text": safe,
                                      "streaming": native.get("event_type") != "item.completed"}
        elif native.get("event_type") == "item.completed":
            messages.pop(identity, None)
    return list(messages.values())[-20:]


def project_child_execution(view: dict, parent_run_id: str) -> dict:
    """Normalize a scoped Native child read to the same detail-panel contract."""
    from types import SimpleNamespace

    child_id = str(view["child_run_id"])
    phase = view.get("phase")
    status = {
        "completed": "completed", "failed": "failed", "canceled": "cancelled",
        "interrupted": "unknown", "paused": "paused", "awaiting_interaction": "paused",
    }.get(phase, "running")
    events = [{"data": {"runtimeEvent": event}} for event in view.get("events", [])]
    projection = project_run_activities(SimpleNamespace(id=child_id, status=status), events)
    return {"runId": parent_run_id, "callId": child_id,
            "label": _safe_text(view.get("role")) or "子智能体",
            "status": status, "childRunId": child_id,
            "facts": projection["activities"],
            "messages": _public_child_messages(events, lambda native_run: native_run == child_id)}


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

    @app.get("/api/v1/runs/{run_id}/activities/stream")
    async def run_activity_stream(run_id: str):
        from fastapi.responses import StreamingResponse

        from ksadk.studio.errors import not_found

        resolved_id = resolve_run_id(run_id) if resolve_run_id else run_id
        resolve_owner = getattr(studio, "runtime_for_run", None)
        owner = resolve_owner(resolved_id) if callable(resolve_owner) else studio
        if owner is None:
            raise not_found("run", resolved_id)
        owner.event_store.get(resolved_id)  # Validate before sending SSE headers.
        return StreamingResponse(stream_activity_snapshots(owner, resolved_id),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

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
        projection = project_run_activities(run, events)
        child_reader = getattr(owner.run_service, "execution_children", None)
        if callable(child_reader):
            for child in await child_reader(resolved_id):
                projection["activities"].append({
                    "id": f"{resolved_id}:native-child:{child['callId']}", "kind": "subagent",
                    "callId": child["callId"], "label": child["label"], "status": child["status"],
                    "details": [],
                })
        return JSONResponse(
            projection, headers={"Cache-Control": "no-store"}
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
            if not detail:
                reader = getattr(owner.run_service, "execution_children", None)
                if callable(reader):
                    children = await reader(resolved_id, child_run_id=call_id)
                    detail = next((child for child in children
                                   if child["callId"] == call_id), {})
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


async def stream_activity_snapshots(owner: Any, run_id: str):
    """Disposable observer. Disconnecting never stops or approves a run.

    Reconnection sends a full idempotent snapshot. Runtime records are cached;
    project only when an event/state changes, or once a second for live clocks.
    """
    import asyncio

    previous = None
    while True:
        run = owner.event_store.get(run_id)
        events = await owner.run_service.events(run_id)
        status = str(getattr(run.status, "value", run.status)).lower()
        marker = (len(events), status, int(time.monotonic()) if status not in _TERMINAL else 0)
        if marker != previous:
            payload = project_run_activities(run, events)
            child_reader = getattr(owner.run_service, "execution_children", None)
            if callable(child_reader):
                for child in await child_reader(run_id):
                    payload["activities"].append({
                        "id": f"{run_id}:native-child:{child['callId']}", "kind": "subagent",
                        "callId": child["callId"], "label": child["label"],
                        "status": child["status"], "details": [],
                    })
            yield f"event: snapshot\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            previous = marker
        if status in _TERMINAL:
            return
        await asyncio.sleep(0.1)
