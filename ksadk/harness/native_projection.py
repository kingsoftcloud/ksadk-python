"""Deterministic canonical v2 projection of committed Native events.

All caches here are disposable projections rebuilt by replay. They cannot
advance a run, approve a tool or change completion/verification outcomes.
"""

from __future__ import annotations

from ksadk.events.canonical import (
    ApprovalRequest,
    ApprovalResponse,
    ErrorInfo,
    InteractionRequested,
    InteractionResolved,
    ItemCompleted,
    ItemSnapshotReplaced,
    ItemStarted,
    ItemUpdated,
    OutputRef,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunProgress,
    RunStarted,
    SourceRef,
    UsageReported,
)
from ksadk.events.content import (
    ContentSnapshot,
    DataContent,
    TextContent,
    ToolCallContent,
    ToolResultContent,
)
from ksadk.events.identity import stable_event_id, stable_item_id, stable_scope_id
from ksadk.harness.native.state import NativeEvent, RunScope
from ksadk.harness.tool_display import safe_tool_display


class NativeEventProjection:
    """Consume a contiguous run ledger, deriving identities without wall clocks."""

    def __init__(self, scope: RunScope, run_id: str) -> None:
        self.run_id = run_id
        self.scope_id = stable_scope_id(
            "ksadk",
            "native",
            scope.tenant_id,
            scope.workspace_id,
            scope.user_id,
            scope.agent_id,
            run_id,
        )
        self.native_seq = 0
        self.seq = 0
        self._drafts: dict[tuple[str, str], tuple[str, str]] = {}
        self._summaries: dict[tuple[str, str], tuple[str, str]] = {}
        self._pending_tools: dict[str, tuple[tuple[str, str, str], ...]] = {}
        self._waiting_tools: dict[str, list[tuple[str, str, str]]] = {}
        self._tool_displays: dict[str, dict] = {}
        self._final_id: str | None = None
        self._usage = dict(
            input_tokens=0, output_tokens=0, total_tokens=0, cached_tokens=0, reasoning_tokens=0
        )
        self._usage_attempts: dict[tuple[str, str], dict] = {}
        self._usage_unavailable_attempts: set[tuple[str, str]] = set()

    def consume(self, native: NativeEvent) -> tuple:
        if native.run_id != self.run_id or native.seq != self.native_seq + 1:
            raise ValueError("native projection requires contiguous same-run history")
        self.native_seq = native.seq
        payload, kind = native.payload, native.kind
        emitted = []

        def emit(cls, *, item_id=None, metadata=None, **fields):
            self.seq += 1
            event_type = cls.model_fields["event_type"].default
            event = cls(
                schema_version=2,
                event_id=stable_event_id(
                    "ksadk",
                    self.scope_id,
                    item_id or "$run",
                    event_type,
                    "native",
                    native.event_id,
                    len(emitted),
                ),
                seq=self.seq,
                run_seq=self.seq,
                timestamp=0.0,
                run_id=self.run_id,
                scope_id=self.scope_id,
                source=SourceRef(
                    framework="ksadk",
                    native_event_id=native.event_id,
                    native_cursor=str(native.seq),
                    native_run_id=self.run_id,
                    native_item_id=item_id,
                    metadata={
                        "engine_kind": "native",
                        "runtime_type": "harness",
                        "native_event_kind": kind,
                        "turn_id": native.turn_id,
                        "timestamp_source": "unavailable",
                        **(metadata or {}),
                    },
                ),
                **({"item_id": item_id} if item_id is not None else {}),
                **fields,
            )
            emitted.append(event)

        def text_snapshot(text):
            return ContentSnapshot(parts=(TextContent(part_id="text", text=text),))

        def close_drafts(operation_key=None, *, retire=False):
            for key, (item, text) in tuple(self._summaries.items()):
                if operation_key is None or key[0] == operation_key:
                    emit(ItemCompleted, item_id=item, item_kind="reasoning",
                         snapshot=text_snapshot(text), metadata={"public_summary": True})
                    self._summaries.pop(key)
            for key, (item, text) in tuple(self._drafts.items()):
                if operation_key is None or key[0] == operation_key:
                    emit(
                        ItemCompleted,
                        item_id=item,
                        item_kind="message",
                        snapshot=text_snapshot("" if retire else text),
                        metadata={
                            "provisional": True,
                            **({"provisional_retired": True} if retire else {}),
                        },
                    )
                    self._drafts.pop(key)

        def tool_receipt(item: str, call_id: str, name: str, status: str) -> None:
            call = ToolCallContent(
                part_id="call", call_id=call_id, name=name, arguments={"redacted": True}
            )
            result = ToolResultContent(
                part_id="result", call_id=call_id, result={"status": status},
                is_error=status in {"error", "uncertain"},
            )
            emit(
                ItemSnapshotReplaced if status == "approval_required" else ItemCompleted,
                item_id=item, item_kind="tool_call", snapshot=ContentSnapshot(parts=(call, result)),
                metadata={
                    "call_id": call_id, "tool_name": name, "tool_status": status,
                    "arguments_redacted": True, "tool_content_redacted": True,
                    **({"tool_display": self._tool_displays[call_id]}
                       if call_id in self._tool_displays else {}),
                },
            )

        def complete_tools(
            operation_key: str, statuses: dict[str, str], fallback: str, *, only: str | None = None,
        ) -> None:
            calls = self._pending_tools.get(operation_key, ())
            remaining = tuple(call for call in calls if only is not None and call[1] != only)
            if remaining:
                self._pending_tools[operation_key] = remaining
            else:
                self._pending_tools.pop(operation_key, None)
            for item, call_id, name in calls:
                if only is not None and call_id != only:
                    continue
                status = statuses.get(call_id, fallback)
                # The physical attempt has a receipt, but an approval suspension
                # has not executed the tool. Keep that UI item open until a real
                # observation (possibly from a resumed operation) or termination.
                if status != "approval_required":
                    for waiting in self._waiting_tools.pop(call_id, ()):
                        if waiting[0] != item:
                            tool_receipt(*waiting, status)
                tool_receipt(item, call_id, name, status)
                if status == "approval_required":
                    waiting = self._waiting_tools.setdefault(call_id, [])
                    if (item, call_id, name) not in waiting:
                        waiting.append((item, call_id, name))

        if kind == "run.started":
            emit(RunStarted, status="running")
        elif kind == "thinking.summary.delta":
            key = (str(payload["request_id"]), str(payload["attempt_id"]))
            text = str(payload["text"])
            if key not in self._summaries:
                item = stable_item_id(
                    "ksadk", self.scope_id, native.turn_id, *key, "public-summary"
                )
                emit(ItemStarted, item_id=item, item_kind="reasoning", phase="commentary",
                     initial=text_snapshot(text), metadata={"public_summary": True})
                self._summaries[key] = (item, text)
            else:
                item, previous = self._summaries[key]
                emit(ItemUpdated, item_id=item, item_kind="reasoning", op="append",
                     update=TextContent(part_id="text", text=text),
                     metadata={"public_summary": True})
                self._summaries[key] = (item, previous + text)
        elif kind == "text.delta":
            # Public assistant content remains separate from explicit summary parts.
            key = (str(payload["request_id"]), str(payload["attempt_id"]))
            text = str(payload["text"])
            if key not in self._drafts:
                item = stable_item_id("ksadk", self.scope_id, native.turn_id, *key, "draft")
                emit(
                    ItemStarted,
                    item_id=item,
                    item_kind="message",
                    phase="commentary",
                    initial=text_snapshot(text),
                    metadata={"provisional": True},
                )
                self._drafts[key] = (item, text)
            else:
                item, previous = self._drafts[key]
                emit(
                    ItemUpdated,
                    item_id=item,
                    item_kind="message",
                    op="append",
                    update=TextContent(part_id="text", text=text),
                    metadata={"provisional": True},
                )
                self._drafts[key] = (item, previous + text)
        elif kind == "text.completed":
            # This event exists only after host finalization, never at candidate
            # production. Final delivery may honestly remain unverified.
            close_drafts()
            if self._final_id is not None:
                raise ValueError("native turn has duplicate final delivery")
            self._final_id = stable_item_id(
                "ksadk", self.scope_id, native.turn_id, "message", "final"
            )
            snapshot = text_snapshot(str(payload["text"]))
            emit(
                ItemStarted,
                item_id=self._final_id,
                item_kind="message",
                phase="final_answer",
                initial=None,
            )
            emit(ItemCompleted, item_id=self._final_id, item_kind="message", snapshot=snapshot)
        elif kind == "usage.reported":
            if payload.get("semantics") != "delta":
                raise ValueError("native usage projection requires explicit delta semantics")
            key = (str(payload["request_id"]), str(payload["attempt_id"]))
            if key in self._usage_attempts:
                if self._usage_attempts[key] != payload:
                    raise ValueError("conflicting usage for one model attempt")
                return ()
            self._usage_attempts[key] = dict(payload)
            for target, source in (
                ("input_tokens", "input_tokens"),
                ("output_tokens", "output_tokens"),
                ("cached_tokens", "cached_input_tokens"),
                ("reasoning_tokens", "reasoning_tokens"),
            ):
                value = payload.get(source, 0)
                if type(value) is not int or value < 0:
                    raise ValueError("invalid native usage")
                self._usage[target] += value
            self._usage["total_tokens"] = self._usage["input_tokens"] + self._usage["output_tokens"]
            # Phase-0's additive mode field is absent in older schema-v2 SDKs.
            # Never inherit its newer default 'delta' for these cumulative totals.
            mode = {"mode": "cumulative"} if "mode" in UsageReported.model_fields else {}
            emit(
                UsageReported,
                **self._usage,
                **mode,
                metadata={
                    "native_usage_semantics": "delta",
                    "usage_semantics": "cumulative",
                    "request_id": key[0],
                    "attempt_id": key[1],
                },
            )
        elif kind == "usage.unavailable":
            if payload.get("semantics") != "delta":
                raise ValueError("native unavailable usage requires explicit delta semantics")
            key = (str(payload["request_id"]), str(payload["attempt_id"]))
            self._usage_unavailable_attempts.add(key)
        elif (
            kind == "effect.prepared"
            and payload.get("kind") == "tool"
            and payload.get("tool_calls")
        ):
            operation_key = str(payload["operation_key"])
            calls = []
            for raw in payload["tool_calls"]:
                call_id, name = str(raw["call_id"]), str(raw["name"])
                display = safe_tool_display(raw.get("tool_display"))
                if display:
                    self._tool_displays[call_id] = display
                item = stable_item_id(
                    "ksadk", self.scope_id, native.turn_id, operation_key, call_id, "tool"
                )
                call = ToolCallContent(
                    part_id="call", call_id=call_id, name=name, arguments={"redacted": True}
                )
                emit(
                    ItemStarted,
                    item_id=item,
                    item_kind="tool_call",
                    initial=ContentSnapshot(parts=(call,)),
                    metadata={
                        "call_id": call_id,
                        "tool_name": name,
                        "tool_status": "running",
                        "arguments_redacted": True,
                        **({"tool_display": display} if display else {}),
                    },
                )
                calls.append((item, call_id, name))
            self._pending_tools[operation_key] = tuple(calls)
        elif kind == "tool.output.updated":
            call_id = str(payload.get("call_id") or "")
            display = safe_tool_display(payload.get("tool_display"))
            matching = next((call for calls in self._pending_tools.values()
                             for call in calls if call[1] == call_id), None)
            if display and matching:
                item, _, name = matching
                self._tool_displays[call_id] = display
                emit(ItemUpdated, item_id=item, item_kind="tool_call", op="replace",
                     update=DataContent(part_id="display", data={"tool_display": display}),
                     metadata={"call_id": call_id, "tool_name": name,
                               "tool_status": "running", "tool_display": display})
        elif (kind in {"tool.member_settled", "tool.child_settled", "tool.child_reserved"}
              and payload.get("model_observation_committed") is True
              and payload.get("status") in {"ok", "error"}):
            # New host events seal one actual observation. Legacy accounting
            # events without this field keep their previous projection.
            call_id = str(payload["call_id"])
            display = safe_tool_display(payload.get("tool_display"))
            if display:
                self._tool_displays[call_id] = display
            complete_tools(str(payload["parent_operation_key"]),
                           {call_id: str(payload["status"])}, "uncertain", only=call_id)
        elif (
            kind == "effect.received"
            and payload.get("kind") == "tool"
            and str(payload.get("operation_key")) in self._pending_tools
        ):
            for row in payload.get("tool_results", ()):
                if isinstance(row, dict) and row.get("call_id"):
                    display = safe_tool_display(row.get("tool_display"))
                    if display:
                        self._tool_displays[str(row["call_id"])] = display
            statuses = {
                str(row["call_id"]): str(row["status"])
                for row in payload.get("tool_results", ())
                if isinstance(row, dict) and row.get("call_id") and row.get("status")
            }
            complete_tools(
                str(payload["operation_key"]),
                statuses,
                "uncertain",
            )
        elif kind == "approval.requested":
            emit(
                InteractionRequested,
                interaction_id=str(payload["approval_id"]),
                interaction_kind="approval",
                request=ApprovalRequest(
                    call_id=str(payload["call_id"]),
                    kind=str(payload["kind"]),
                    detail=payload.get("detail"),
                ),
            )
        elif kind == "approval.resolved":
            decision = {"allow": "approved", "deny": "rejected", "canceled": "canceled"}[
                str(payload["decision"])
            ]
            emit(
                InteractionResolved,
                interaction_id=str(payload["approval_id"]),
                interaction_kind="approval",
                response=ApprovalResponse(decision=decision),
                metadata={"call_id": str(payload.get("call_id") or "")},
            )
        elif kind in {"run.completed", "run.failed", "run.canceled", "run.interrupted"}:
            close_drafts()
            for operation_key in tuple(self._pending_tools):
                complete_tools(operation_key, {}, "uncertain")
            for waiting_items in tuple(self._waiting_tools.values()):
                for waiting in waiting_items:
                    tool_receipt(*waiting, "uncertain")
            self._waiting_tools.clear()
            metadata = {
                key: payload[key]
                for key in ("outcome", "reason", "contract_revision", "verification_report_ref")
                if key in payload
            }
            verification = payload.get("verification")
            if isinstance(verification, dict) and isinstance(verification.get("items"), list):
                metadata["verification_items"] = [
                    {"acceptance_id": row["acceptance_id"], "status": row["status"]}
                    for row in verification["items"]
                    if isinstance(row, dict)
                    and isinstance(row.get("acceptance_id"), str)
                    and row.get("status") in {"passed", "failed", "not_run", "blocked"}
                ]
            metadata["usage_completeness"] = (
                "partial" if self._usage_unavailable_attempts else "complete"
            )
            metadata["usage_unavailable_attempts"] = len(self._usage_unavailable_attempts)
            if kind == "run.completed":
                refs = (
                    ()
                    if self._final_id is None
                    else (OutputRef(scope_id=self.scope_id, item_id=self._final_id),)
                )
                emit(RunCompleted, status="completed", output_refs=refs, metadata=metadata)
            elif kind == "run.failed":
                emit(
                    RunFailed,
                    status="failed",
                    metadata=metadata,
                    error=ErrorInfo(
                        code=str(payload.get("reason") or "native_run_failed"),
                        message=(
                            "已达到本轮执行时限，任务尚未完成。"
                            if payload.get("reason") == "wall_time_hard_limit"
                            else "本轮图片超出已配置的输入额度，请减少图片数量或大小。"
                            if payload.get("reason") == "request_current_media_over_budget"
                            else None
                        ),
                        source="ksadk.harness.native",
                        scope_id=self.scope_id,
                    ),
                )
            elif kind == "run.canceled":
                emit(
                    RunCanceled, status="canceled", reason=payload.get("reason"), metadata=metadata
                )
            else:
                emit(
                    RunInterrupted,
                    status="interrupted",
                    reason=payload.get("reason"),
                    metadata=metadata,
                )
        elif (
            kind == "command.consumed"
            and payload.get("reason") == "applied"
            and payload.get("kind") in {"pause", "resume"}
        ):
            if payload["kind"] == "pause":
                emit(RunInterrupted, status="interrupted", reason="paused")
            else:
                emit(RunProgress, status="running")
        else:
            if kind == "effect.received" and payload.get("kind") == "model":
                # A sealed tool response may contain actual commentary. Final
                # candidates, continuations and failed attempts are previews;
                # their durable public snapshot must not duplicate delivery.
                # Raw deltas and receipts remain in the authoritative ledger.
                close_drafts(
                    str(payload["operation_key"]),
                    retire=(
                        payload.get("provisional_display_policy") == "native.provisional-display/v1"
                        and payload.get("model_response_kind") != "tool_calls"
                    ),
                )
            # Execution/compaction facts stay in trace. Private reasoning, tool
            # instructions and mutable snapshots never become chat.
            item = stable_item_id("ksadk", self.scope_id, native.event_id, "status")
            data = {
                "native_event_kind": kind,
                **{
                    key: payload[key]
                    for key in (
                        "operation_key",
                        "kind",
                        "status",
                        "reason",
                        "phase",
                        "outcome",
                        "command_id",
                        "child_run_id",
                        "child_input_id",
                        "model_purpose",
                        "model_response_kind",
                    )
                    if key in payload
                },
            }
            if kind == "model.input_observed":
                # Only the Kernel's sanitized observation payload crosses UI;
                # original paths/unknown fields remain in the private receipt.
                data.update({key: payload[key] for key in (
                    "request_id", "attempt_id", "receipt_digest", "provider_prefix_digest",
                    "source", "known_counts", "unknown_count",
                ) if key in payload})
            if kind == "model.retry_scheduled":
                data.update({key: payload[key] for key in (
                    "failure_category", "next_attempt", "max_attempts", "delay_ms",
                ) if key in payload})
            snapshot = ContentSnapshot(parts=(DataContent(part_id="status", data=data),))
            emit(ItemStarted, item_id=item, item_kind="status", initial=None)
            emit(ItemCompleted, item_id=item, item_kind="status", snapshot=snapshot)
        return tuple(emitted)
