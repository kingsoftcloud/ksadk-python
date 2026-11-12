"""Runner chunk projections, including legacy tool approval compatibility.

从 ``ksadk.runtime.runner_adapter`` 按职责拆出(纯移动,行为不变)。以 mixin 形式
被 :class:`RunnerRuntimeAdapter` 继承,依赖宿主提供 ``_active_runs`` /
``_runtime_type`` / ``_runner`` / ``_next_seq`` / ``_canonical_kwargs`` /
``_interaction_requested_from_approval`` / ``_coerce``。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Optional, cast

from pydantic import JsonValue

from ksadk.events.canonical import (
    ApprovalRequest,
    ContentSnapshot,
    ContinuationCreated,
    ErrorInfo,
    EventEnvelope,
    InteractionRequested,
    ItemCompleted,
    ItemStarted,
    ItemUpdated,
    RunFailed,
    RunProgress,
    RuntimeEvent,
    SourceRef,
    UsageReported,
)
from ksadk.events.content import DataContent, TextContent, ToolCallContent, ToolResultContent
from ksadk.events.identity import stable_item_id, stable_scope_id
from ksadk.runtime._runner_adapter.a2ui_surface import _a2ui_surface_event as _a2ui_surface_event
from ksadk.runtime.adapter import RunHandle
from ksadk.runtime.usage import canonical_usage_payload
from ksadk.tools.gateway import approval_interrupt_info_from_result

if TYPE_CHECKING:
    from ksadk.runtime.runner_adapter import _ActiveRun

logger = logging.getLogger(__name__)

_STREAM_STOP = object()


class RunnerChunkMappingMixin:
    def _canonical_gateway_approvals(
        self, handle: RunHandle, run: Optional[_ActiveRun], event: ItemCompleted
    ) -> list[RuntimeEvent]:
        """Recognize local results without treating historical state as new calls."""
        calls = handle.native_ref.setdefault("_gateway_canonical_calls", {})
        historical_calls = handle.native_ref.setdefault("_gateway_historical_result_calls", {})

        def key(call_id):
            return json.dumps([event.run_id, event.scope_id, call_id])

        results = []
        for part in event.snapshot.parts:
            if isinstance(part, ToolCallContent):
                calls[key(part.call_id)] = {"name": part.name, "arguments": part.arguments}
            elif isinstance(part, ToolResultContent):
                results.append((part.call_id, part.result))
            elif (
                isinstance(part, DataContent)
                and event.source.metadata.get("channel") in {"updates", "values"}
                and isinstance(part.data, dict)
            ):
                # Some ToolNode versions only emit values. Remember already
                # present ToolMessage IDs before admitting new native calls,
                # so loading a checkpoint cannot reopen historical approvals.
                channel = event.source.metadata.get("channel")
                updates = [part.data] if channel == "values" else part.data.values()
                seen = handle.native_ref.setdefault("_gateway_canonical_results", {})
                for update in updates:
                    if not isinstance(update, dict):
                        continue
                    messages = update.get("messages", [])
                    if not isinstance(messages, list):
                        messages = [messages]
                    for message in messages:
                        if not isinstance(message, dict) or message.get("type") != "tool":
                            continue
                        call_id = message.get("tool_call_id")
                        if channel == "values" and call_id and key(call_id) not in calls:
                            # A restored snapshot may omit the ToolMessage id.
                            # Its native call identity still fences later typed
                            # replays in this Run and scope.
                            historical_calls[key(call_id)] = True
                        message_id = message.get("id")
                        if not message_id:
                            continue
                        result_key = key(message_id)
                        if result_key in seen:
                            continue
                        seen[result_key] = True
                        if call_id and key(call_id) not in calls:
                            # This result was already present before a live
                            # call. Replayed native result parts must not
                            # reopen an approval from the restored snapshot.
                            historical_calls[key(call_id)] = True
                        results.append((call_id, message.get("content")))
        approvals = []
        for call_id, result in results:
            if key(call_id) in historical_calls:
                continue
            call = calls.get(key(call_id))
            if call is None:
                continue
            detail = approval_interrupt_info_from_result(
                result, fallback_tool_name=call["name"],
                tool_args=call["arguments"], run_id=call_id,
            )
            if detail is None:
                continue
            approval_id = str(detail.get("approval_request_id") or detail.get("id") or call_id)
            if approval_id in handle.native_ref.get("pending_approval_ids", []):
                continue
            approvals.extend(self._interaction_requested_from_approval(
                handle, run, detail=detail, call_id=call_id,
            ))
        return approvals

    def _chunk_to_event(
        self, handle: RunHandle, run: Optional[_ActiveRun], chunk: Any
    ) -> list[RuntimeEvent]:
        if isinstance(chunk, EventEnvelope):
            # canonical 事件(来自 stream_canonical_events):直接转发,
            # 抑制 runner 自己的 run.started(adapter 已发自己的)。
            if chunk.event_type == "run.started":
                return []
            if (
                self._runtime_type == "langgraph"
                and chunk.source.framework == "langgraph"
                and isinstance(chunk, ContinuationCreated)
                and chunk.continuation_kind == "graph_checkpoint"
            ):
                # The canonical continuation arrives before its interaction.
                # Retain its native target just as the legacy checkpoint path
                # does, so Kernel records and resumes the original interrupt.
                checkpoint_id = chunk.ref.get("checkpoint_id")
                thread_id = chunk.ref.get("thread_id")
                if (
                    isinstance(checkpoint_id, str)
                    and checkpoint_id
                    and isinstance(thread_id, str)
                    and thread_id
                ):
                    native_ref = {key: chunk.ref[key] for key in (
                        "checkpoint_id", "thread_id", "checkpoint_ns"
                    ) if key in chunk.ref}
                    handle.native_ref.update(native_ref)
                    handle.native_ref["framework_ref"] = {"langgraph": native_ref}
                    known = handle.native_ref.setdefault("known_checkpoint_ids", [])
                    if checkpoint_id not in known:
                        known.append(checkpoint_id)
            if (
                self._runtime_type == "langgraph"
                and chunk.source.framework == "langgraph"
                and isinstance(chunk, ItemCompleted)
            ):
                approvals = self._canonical_gateway_approvals(handle, run, chunk)
                if approvals:
                    return [chunk, *approvals]
            return [chunk]
        if not isinstance(chunk, dict):
            chunk = {"type": "text", "delta": str(chunk)}

        framework = self._runtime_type  # type: ignore[attr-defined]
        run_id = handle.run_id
        scope_id = stable_scope_id(framework, run_id)
        started = run.started_items if run is not None else set()

        def ensure_started(
            *,
            item_id: str,
            item_kind: str,
            phase: str | None = None,
            initial: ContentSnapshot | None = None,
        ) -> list[RuntimeEvent]:
            key = (scope_id, item_id)
            if key in started:
                return []
            started.add(key)
            return [
                ItemStarted(
                    **self._canonical_kwargs(  # type: ignore[attr-defined]
                        handle,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="item.started",
                        part_id="item",
                    ),
                    item_id=item_id,
                    item_kind=item_kind,
                    phase=phase,
                    initial=initial,
                )
            ]

        def tool_item_id(effective_call_id: str, item_kind: str) -> str:
            components = (effective_call_id, item_kind)
            if run is None:
                return stable_item_id(framework, run_id, *components)
            occurrences = run.__dict__.setdefault("_tool_item_occurrences", {})
            occurrence_key = (scope_id, item_kind, effective_call_id)
            occurrence = int(occurrences.get(occurrence_key, 0))
            occurrences[occurrence_key] = occurrence + 1
            if occurrence == 0:
                return stable_item_id(framework, run_id, *components)
            return stable_item_id(
                framework,
                run_id,
                *components,
                "occurrence",
                occurrence,
            )

        chunk_type = chunk.get("type")

        # ---- reasoning ----
        if chunk_type in ("reasoning", "reasoning_delta", "thinking"):
            text = self._coerce(  # type: ignore[attr-defined]
                chunk.get("delta")
                or chunk.get("content")
                or chunk.get("output")
                or chunk.get("data")
            )
            if not text:
                return []
            item_id = stable_item_id(framework, run_id, "reasoning")
            events: list[RuntimeEvent] = ensure_started(
                item_id=item_id, item_kind="reasoning", phase="commentary"
            )
            if chunk.get("status") in ("completed", "done"):
                events.append(
                    ItemCompleted(
                        **self._canonical_kwargs(  # type: ignore[attr-defined]
                            handle,
                            scope_id=scope_id,
                            item_id=item_id,
                            event_type="item.completed",
                            part_id="reasoning-text",
                        ),
                        item_id=item_id,
                        item_kind="reasoning",
                        snapshot=ContentSnapshot(
                            parts=(TextContent(part_id="reasoning-text", text=text),)
                        ),
                    )
                )
            else:
                events.append(
                    ItemUpdated(
                        **self._canonical_kwargs(  # type: ignore[attr-defined]
                            handle,
                            scope_id=scope_id,
                            item_id=item_id,
                            event_type="item.updated",
                            part_id="reasoning-text",
                        ),
                        item_id=item_id,
                        item_kind="reasoning",
                        op="append",
                        update=TextContent(part_id="reasoning-text", text=text),
                    )
                )
            return events

        # ---- tool_call ----
        if chunk_type in ("tool_call", "tool_start"):
            call_id = str(
                chunk.get("tool_call_id")
                or chunk.get("call_id")
                or chunk.get("run_id")
                or chunk.get("id")
                or ""
            )
            name = str(chunk.get("tool_name") or chunk.get("name") or "tool")
            effective_call_id = call_id or name
            item_id = tool_item_id(effective_call_id, "tool_call")
            part_id = "tool_call"
            tc_content = ToolCallContent(
                part_id=part_id,
                call_id=effective_call_id,
                name=name,
                arguments=cast(JsonValue, chunk.get("tool_args", chunk.get("args")) or {}),
            )
            return [
                ItemStarted(
                    **self._canonical_kwargs(  # type: ignore[attr-defined]
                        handle,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="item.started",
                        part_id=part_id,
                    ),
                    item_id=item_id,
                    item_kind="tool_call",
                    phase="commentary",
                    initial=ContentSnapshot(parts=(tc_content,)),
                ),
                ItemCompleted(
                    **self._canonical_kwargs(  # type: ignore[attr-defined]
                        handle,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="item.completed",
                        part_id=part_id,
                    ),
                    item_id=item_id,
                    item_kind="tool_call",
                    snapshot=ContentSnapshot(parts=(tc_content,)),
                ),
            ]

        # ---- tool_result ----
        if chunk_type in ("tool_result", "tool_end"):
            call_id = str(
                chunk.get("tool_call_id")
                or chunk.get("call_id")
                or chunk.get("run_id")
                or chunk.get("id")
                or ""
            )
            name = str(chunk.get("tool_name") or chunk.get("name") or "tool")
            effective_call_id = call_id or name
            item_id = tool_item_id(effective_call_id, "tool_result")
            part_id = "tool_result"
            result_data = chunk.get("tool_output", chunk.get("output"))
            # ToolGateway 审批可能出现在"本已终态"的 tool result 里;识别后转为
            # canonical InteractionRequested(语义续跑由 runner 的
            # supports_gateway_approval_semantic_resume 决定)。
            tool_args = chunk.get("tool_args", chunk.get("args"))
            approval_detail = approval_interrupt_info_from_result(
                result_data,
                fallback_tool_name=name,
                tool_args=tool_args,
                run_id=call_id or None,
            )
            if approval_detail is not None:
                return self._interaction_requested_from_approval(  # type: ignore[attr-defined]
                    handle,
                    run,
                    detail=approval_detail,
                    call_id=call_id,
                )
            tr_content = ToolResultContent(
                part_id=part_id,
                call_id=effective_call_id,
                result=cast(JsonValue, result_data if result_data is not None else {}),
                is_error=bool(chunk.get("error")),
            )
            return [
                ItemStarted(
                    **self._canonical_kwargs(  # type: ignore[attr-defined]
                        handle,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="item.started",
                        part_id=part_id,
                    ),
                    item_id=item_id,
                    item_kind="tool_result",
                    phase="commentary",
                ),
                ItemCompleted(
                    **self._canonical_kwargs(  # type: ignore[attr-defined]
                        handle,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="item.completed",
                        part_id=part_id,
                    ),
                    item_id=item_id,
                    item_kind="tool_result",
                    snapshot=ContentSnapshot(parts=(tr_content,)),
                ),
            ]

        # ---- interrupt / approval ----
        if chunk_type in ("interrupt", "approval", "approval_required"):
            detail = chunk.get("interrupt_info") or chunk.get("detail") or {}
            detail_id = detail.get("approval_request_id") if isinstance(detail, dict) else None
            call_id = str(
                chunk.get("call_id")
                or chunk.get("approval_id")
                or chunk.get("id")
                or detail_id
                or ""
            )
            if run is not None and call_id:
                run.pending_approvals.add(call_id)
            if call_id:
                pending_approval_ids = handle.native_ref.setdefault("pending_approval_ids", [])
                if call_id not in pending_approval_ids:
                    pending_approval_ids.append(call_id)
            interaction_id = call_id or stable_item_id(framework, run_id, "interaction")
            item_id = stable_item_id(framework, run_id, "interaction")
            detail_value: JsonValue = (
                cast(JsonValue, detail)
                if isinstance(detail, (dict, list, str, int, float, bool, type(None)))
                else None
            )
            return [
                InteractionRequested(
                    **self._canonical_kwargs(  # type: ignore[attr-defined]
                        handle,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="interaction.requested",
                        part_id="interaction",
                    ),
                    interaction_id=interaction_id,
                    interaction_kind="approval",
                    request=ApprovalRequest(
                        call_id=call_id or None,
                        kind="tool",
                        detail=detail_value,
                    ),
                )
            ]

        # ---- checkpoint ----
        if chunk_type == "checkpoint":
            raw_metadata = chunk.get("metadata")
            metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
            raw_agentengine = metadata.get("agentengine")
            agentengine: dict[str, Any] = (
                raw_agentengine if isinstance(raw_agentengine, dict) else {}
            )
            ckpt_framework = str(agentengine.get("framework") or self._runtime_type)  # type: ignore[attr-defined]
            framework_ref = agentengine.get("framework_ref") or {}
            runtime_ref = (
                framework_ref.get(ckpt_framework) if isinstance(framework_ref, dict) else {}
            ) or {}
            checkpoint_id = str(
                runtime_ref.get("checkpoint_id") if isinstance(runtime_ref, dict) else ""
            )
            if not checkpoint_id:
                return []
            handle.native_ref["checkpoint_id"] = checkpoint_id
            known_checkpoint_ids = handle.native_ref.setdefault("known_checkpoint_ids", [])
            if checkpoint_id not in known_checkpoint_ids:
                known_checkpoint_ids.append(checkpoint_id)
            handle.native_ref["framework_ref"] = framework_ref
            if isinstance(runtime_ref, dict):
                handle.native_ref.update(runtime_ref)
            item_id = stable_item_id(framework, run_id, "$run")
            # Extract checkpoint capability from agentengine metadata so the
            # canonical event carries backend/scope/next_node facts that the
            # projection layer (projection.py) needs for the REST payload.
            capability: dict[str, Any] = {}
            for cap_key in (
                # Capability / resume fields
                "backend", "scope", "durable", "next_node",
                "is_terminal", "is_resumable", "resume_status",
                "resume_disabled_reason",
                # Checkpoint display fields
                "phase", "stage", "stage_name", "stage_key",
                "stage_index", "total_stages", "summary",
                "next_action", "status", "artifact_preview",
                "tool_name", "receipt_key", "artifact_path",
            ):
                cap_val = agentengine.get(cap_key)
                if cap_val is not None:
                    capability[cap_key] = cap_val
            canonical_kwargs = self._canonical_kwargs(  # type: ignore[attr-defined]
                handle,
                scope_id=scope_id,
                item_id=item_id,
                event_type="continuation.created",
                part_id="continuation",
            )
            if capability:
                base_source = canonical_kwargs["source"]
                canonical_kwargs["source"] = SourceRef(
                    framework=base_source.framework,
                    protocol=base_source.protocol,
                    native_run_id=base_source.native_run_id,
                    metadata={**base_source.metadata, "capability": capability},
                )
            ref_value = cast(
                JsonValue,
                framework_ref if isinstance(framework_ref, dict) else {},
            )
            next_node_val = agentengine.get("next_node")
            return [
                ContinuationCreated(
                    **canonical_kwargs,
                    continuation_id=checkpoint_id,
                    continuation_kind="graph_checkpoint",
                    resumable=bool(agentengine.get("is_resumable", True)),
                    ref={
                        "framework": ckpt_framework,
                        "framework_ref": ref_value,
                        "resume_target": ref_value,
                        **({"next_node": str(next_node_val)} if next_node_val else {}),
                    },
                )
            ]

        # ---- graph_update ----
        if chunk_type == "graph_update":
            item_id = stable_item_id(framework, run_id, "$run")
            return [
                RunProgress(
                    **self._canonical_kwargs(  # type: ignore[attr-defined]
                        handle,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="run.progress",
                        part_id="run",
                    ),
                    status="running",
                    message=str(chunk.get("node") or ""),
                )
            ]

        # ---- usage ----
        if chunk_type == "usage":
            raw_usage = chunk.get("usage")
            usage_dict = (
                canonical_usage_payload(raw_usage, runtime_type=framework)
                if isinstance(raw_usage, Mapping)
                else {}
            )
            item_id = stable_item_id(framework, run_id, "$run")
            return [
                UsageReported(
                    **self._canonical_kwargs(  # type: ignore[attr-defined]
                        handle,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="usage.reported",
                        part_id="usage",
                    ),
                    input_tokens=int(usage_dict.get("input_tokens") or 0),
                    output_tokens=int(usage_dict.get("output_tokens") or 0),
                    total_tokens=int(usage_dict.get("total_tokens") or 0),
                    cached_tokens=int(usage_dict.get("cached_tokens") or 0),
                    reasoning_tokens=int(usage_dict.get("reasoning_tokens") or 0),
                )
            ]

        # ---- error ----
        if chunk_type == "error":
            error = self._coerce(chunk.get("message") or chunk.get("error"))  # type: ignore[attr-defined]
            item_id = stable_item_id(framework, run_id, "$run")
            return [
                RunFailed(
                    **self._canonical_kwargs(  # type: ignore[attr-defined]
                        handle,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="run.failed",
                        part_id="run",
                    ),
                    status="failed",
                    error=ErrorInfo(
                        code="runner_failed",
                        message=error or "runner failed",
                        source=framework,
                        scope_id=scope_id,
                    ),
                )
            ]

        # ---- final ----
        if chunk_type == "final":
            output = self._coerce(chunk.get("output"))  # type: ignore[attr-defined]
            item_id = stable_item_id(framework, run_id, "message", "final_answer")
            if run is not None:
                run.final_answer_item_id = item_id
            text_content = TextContent(part_id="text-0", text=output)
            # Auto-close any open explicit commentary/reasoning item before completing
            # final_answer. Text deltas already belong to the final-answer item and must
            # keep that identity through completion.
            # Commentary/thinking deltas create items that are never ItemCompleted;
            # without this, RunCompleted fails _ensure_no_open_items.
            #
            # Close them *before* allocating the final-answer item.  Event
            # constructors allocate ``seq`` eagerly, so inserting a later
            # completion at index zero would otherwise return an event list
            # whose physical order disagrees with its sequence numbers.
            events: list[RuntimeEvent] = []
            for close_kind, close_part_id, close_components in (
                ("message", "text-0", ("message", "commentary")),
                ("reasoning", "reasoning-text", ("reasoning",)),
            ):
                close_item_id = stable_item_id(framework, run_id, *close_components)
                close_key = (scope_id, close_item_id)
                if close_key in started:
                    started.discard(close_key)
                    events.append(
                        ItemCompleted(
                            **self._canonical_kwargs(  # type: ignore[attr-defined]
                                handle,
                                scope_id=scope_id,
                                item_id=close_item_id,
                                event_type="item.completed",
                                part_id=close_part_id,
                            ),
                            item_id=close_item_id,
                            item_kind=close_kind,
                            snapshot=ContentSnapshot(
                                parts=(TextContent(part_id=close_part_id, text=""),)
                            ),
                        ),
                    )
            events.extend(
                ensure_started(item_id=item_id, item_kind="message", phase="final_answer")
            )
            # The dict-chunk path is still required for gateway approval flows.
            # It must project runner-final usage into the canonical stream too;
            # otherwise non-streaming Runs aggregate zero even when the runner
            # returned provider token accounting.
            raw_usage = chunk.get("usage")
            if isinstance(raw_usage, Mapping):
                usage_dict = canonical_usage_payload(
                    raw_usage,
                    runtime_type=framework,
                )
                input_tokens = usage_dict["input_tokens"]
                output_tokens = usage_dict["output_tokens"]
                total_tokens = usage_dict["total_tokens"]
                if input_tokens or output_tokens or total_tokens:
                    events.append(
                        UsageReported(
                            **self._canonical_kwargs(  # type: ignore[attr-defined]
                                handle,
                                scope_id=scope_id,
                                item_id=stable_item_id(framework, run_id, "$run"),
                                event_type="usage.reported",
                                part_id="usage",
                            ),
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            total_tokens=total_tokens,
                            cached_tokens=usage_dict["cached_tokens"],
                            reasoning_tokens=usage_dict["reasoning_tokens"],
                        )
                    )
            events.append(
                ItemCompleted(
                    **self._canonical_kwargs(  # type: ignore[attr-defined]
                        handle,
                        scope_id=scope_id,
                        item_id=item_id,
                        event_type="item.completed",
                        part_id="text-0",
                    ),
                    item_id=item_id,
                    item_kind="message",
                    snapshot=ContentSnapshot(parts=(text_content,)),
                )
            )
            return events

        # ---- default: text delta ----
        text = self._coerce(chunk.get("delta") or chunk.get("output") or chunk.get("data"))  # type: ignore[attr-defined]
        if not text:
            return []
        is_commentary = chunk_type in {"commentary", "commentary_delta"}
        phase = "commentary" if is_commentary else "final_answer"
        item_id = stable_item_id(framework, run_id, "message", phase)
        if run is not None and not is_commentary:
            run.final_answer_item_id = item_id
        op: str = "replace" if chunk.get("replace") else "append"
        events = ensure_started(item_id=item_id, item_kind="message", phase=phase)
        events.append(
            ItemUpdated(
                **self._canonical_kwargs(  # type: ignore[attr-defined]
                    handle,
                    scope_id=scope_id,
                    item_id=item_id,
                    event_type="item.updated",
                    part_id="text-0",
                ),
                item_id=item_id,
                item_kind="message",
                op=op,
                update=TextContent(part_id="text-0", text=text),
            )
        )
        return events
