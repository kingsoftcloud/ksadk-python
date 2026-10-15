"""Identity-aware ADK stream lifecycle and legacy chunk projection."""

from __future__ import annotations

import time
import logging
import traceback
from pathlib import Path
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

from ksadk.events.adapters.adk import ADKAdapterContext, ADKEventAdapter
from ksadk.events.canonical import (
    ApprovalRequest,
    ErrorInfo,
    InteractionRequested,
    ItemCompleted,
    ItemFailed,
    ItemSnapshotReplaced,
    ItemStarted,
    ItemUpdated,
    OutputRef,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunStarted,
    RuntimeEvent,
    SourceRef,
    StructuredInputRequest,
    UsageReported,
)
from ksadk.events.content import DataContent, TextContent, ToolCallContent, ToolResultContent
from ksadk.events.identity import stable_event_id, stable_scope_id
from ksadk.runners.usage_accumulator import accumulate_usage
from ksadk.tools.gateway import approval_interrupt_info_from_result


async def canonical_events(
    runner: Any,
    native_events: AsyncIterator[Any],
    *,
    run_id: str,
    session_id: str,
    span: Any,
) -> AsyncGenerator[RuntimeEvent, None]:
    """Map one native ADK invocation to a complete canonical run."""

    context = ADKAdapterContext(run_id=run_id)
    adapter = ADKEventAdapter()
    root_scope_id = stable_scope_id("adk", run_id)
    usage: dict[str, Any] = {}
    last_usage: dict[str, Any] = {}
    approval_ids: set[str] = set()
    message_snapshots: dict[tuple[str, str], dict[str, str]] = {}
    managed_descriptors = {}
    root_transfer_target = None

    def envelope(event_type: str, occurrence: str = "run") -> dict[str, Any]:
        return {
            "schema_version": 2,
            "run_id": run_id,
            "scope_id": root_scope_id,
            "event_id": stable_event_id(
                "adk",
                root_scope_id,
                "run",
                event_type,
                "run",
                occurrence,
                0,
            ),
            "seq": context.allocate_seq(),
            "timestamp": time.time(),
            "source": SourceRef(
                framework="adk",
                metadata={
                    "session_id": session_id,
                    **({"last_usage": last_usage} if last_usage else {}),
                    **({"usage": usage} if usage else {}),
                },
            ),
        }

    stream_failure: str | None = None
    stream_failure_code = "adk_stream_error"
    yield RunStarted(**envelope("run.started"), status="running")
    try:
        async for native_event in native_events:
            if getattr(native_event, "error_code", None):
                raise RuntimeError("ADK model returned an error")
            if getattr(native_event, "author", None) == getattr(runner._agent, "name", None):
                target = getattr(getattr(native_event, "actions", None), "transfer_to_agent", None)
                if target:
                    root_transfer_target = target

            event_usage = runner._extract_event_usage(native_event)
            if event_usage:
                usage = accumulate_usage(usage, event_usage)
                last_usage = event_usage

            approval_signals = list(runner._extract_approval_signals(native_event))
            for event in adapter.map(native_event, context):
                if isinstance(event, ItemCompleted) and event.item_kind in {"message", "artifact"}:
                    message_snapshots[(event.scope_id, event.item_id)] = {
                        part.part_id: part.text
                        for part in event.snapshot.parts
                        if isinstance(part, TextContent)
                    }
                if (
                    isinstance(event, (ItemStarted, ItemSnapshotReplaced, ItemCompleted))
                    and event.item_kind == "status"
                    and getattr(native_event, "_canonical", None) is event
                ):
                    from ksadk.a2a.presentation import ExecutionScopeDescriptor

                    snapshot = event.initial if isinstance(event, ItemStarted) else event.snapshot
                    for part in snapshot.parts:
                        if (
                            isinstance(part, DataContent)
                            and isinstance(part.data, dict)
                            and (part.data.get("schema") or part.data.get("schema_"))
                            == "execution.scope/v1"
                        ):
                            descriptor = ExecutionScopeDescriptor.model_validate(part.data)
                            managed_descriptors[descriptor.scope_id] = descriptor
                if isinstance(event, ItemCompleted) and event.item_kind == "tool_result":
                    for part in event.snapshot.parts:
                        if not isinstance(part, ToolResultContent):
                            continue
                        approval = approval_interrupt_info_from_result(
                            part.result,
                            fallback_tool_name=str(event.source.metadata.get("tool_name", "tool")),
                            run_id=part.call_id,
                        )
                        if approval:
                            approval_signals.append(approval)
                yield event

            for signal in approval_signals:
                approval_id = str(signal.get("approval_request_id") or "")
                if not approval_id or approval_id in approval_ids:
                    continue
                approval_ids.add(approval_id)
                call_id = str(signal.get("call_id") or signal.get("run_id") or approval_id)
                interaction_fields = envelope("interaction.requested", approval_id)
                native_run_id = str(getattr(native_event, "invocation_id", "") or "")
                if native_run_id:
                    interaction_fields["source"] = interaction_fields["source"].model_copy(
                        update={"native_run_id": native_run_id}
                    )
                if signal.get("kind") == "input":
                    response_schema = signal.get("response_schema")
                    yield InteractionRequested(
                        **interaction_fields,
                        interaction_id=approval_id,
                        interaction_kind="structured_input",
                        request=StructuredInputRequest(
                            prompt=str(signal.get("message") or "") or None,
                            schema=(
                                dict(response_schema)
                                if isinstance(response_schema, dict)
                                else {}
                            ),
                        ),
                    )
                else:
                    yield InteractionRequested(
                        **interaction_fields,
                        interaction_id=approval_id,
                        interaction_kind="approval",
                        request=ApprovalRequest(
                            call_id=call_id,
                            kind=str(signal.get("kind") or "tool"),
                            detail=signal,
                        ),
                    )
    except Exception as exc:  # noqa: BLE001
        if getattr(runner, "_managed_a2a_bootstrap", None) is not None:
            # Keep diagnostic locations server-side. Never log exception text,
            # source lines or local values: provider errors can embed credentials.
            frames = traceback.extract_tb(exc.__traceback__)
            frames = frames if len(frames) <= 24 else frames[:12] + frames[-12:]
            locations = ";".join(
                f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                for frame in frames
            )
            copy_types = []
            for frame, _line in traceback.walk_tb(exc.__traceback__):
                if (
                    frame.f_code.co_name == "deepcopy"
                    and Path(frame.f_code.co_filename).name == "copy.py"
                ):
                    value_type = type(frame.f_locals.get("x"))
                    copy_types.append(f"{value_type.__module__}.{value_type.__qualname__}")
            logging.getLogger(__name__).error(
                "Managed A2A ADK stream failure run=%s type=%s frames=%s copy_types=%s",
                run_id, type(exc).__name__, locations, ";".join(copy_types[:24]),
            )
        stream_failure = f"ADK stream failed ({type(exc).__name__})"
        if isinstance(exc, ValueError) and exc.args == ("A2A_TERMINAL_CONTENT_CONFLICT",):
            stream_failure_code = "A2A_TERMINAL_CONTENT_CONFLICT"

    for event in adapter.finish(context):
        if isinstance(event, ItemCompleted) and event.item_kind == "message":
            message_snapshots[(event.scope_id, event.item_id)] = {
                part.part_id: part.text
                for part in event.snapshot.parts
                if isinstance(part, TextContent)
            }
        yield event

    incomplete_stream = bool(context.open_items)
    for (scope_id, item_id), item_kind in context.open_item_kinds.items():
        error = ErrorInfo(
            code="incomplete_stream",
            message="ADK item stream ended before completion",
            source="adk",
            scope_id=scope_id,
            item_id=item_id,
        )
        fields = envelope("item.failed", item_id)
        fields["scope_id"] = scope_id
        yield ItemFailed(
            **fields,
            item_id=item_id,
            item_kind=item_kind,
            error=error,
        )
        context.mark_failed((scope_id, item_id))

    if usage:
        yield UsageReported(
            **envelope("usage.reported"),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
            cached_tokens=int(usage.get("cached_tokens") or 0),
            reasoning_tokens=int(usage.get("reasoning_tokens") or 0),
        )

    output_refs = context.output_refs
    if not output_refs and managed_descriptors:
        binding = _delegated_binding(runner._agent, root_transfer_target)
        selected = next(
            (
                value
                for value in reversed(tuple(managed_descriptors.values()))
                if value.binding_id == binding
            ),
            None,
        )
        if selected is not None and selected.status == "completed":
            output_refs = tuple(OutputRef(**ref.model_dump()) for ref in selected.output_refs)
        elif selected is not None and not stream_failure:
            stream_failure = "Delegated remote execution did not complete successfully"
            stream_failure_code = (
                "A2A_DELEGATE_FAILED"
                if selected.status in {"failed", "cancelled"}
                else "A2A_DELEGATE_OUTCOME_UNCONFIRMED"
            )
    if not stream_failure and any(
        value.status not in {"completed", "failed", "cancelled"}
        for value in managed_descriptors.values()
    ):
        # The native iterator ended, but the remote Task has no terminal proof.
        # Fail the local run without changing the child's durable remote state.
        stream_failure = "Remote execution outcome remains unconfirmed"
        stream_failure_code = "A2A_DELEGATE_OUTCOME_UNCONFIRMED"
    output = _selected_text(message_snapshots, output_refs)
    span.set_attribute("output.value", output[:5000])
    span.set_attribute("agent.output", output[:500])

    if stream_failure:
        yield RunFailed(
            **envelope("run.failed"),
            status="failed",
            error=ErrorInfo(
                code=stream_failure_code,
                message=stream_failure,
                source="adk",
                scope_id=root_scope_id,
            ),
        )
    elif approval_ids:
        yield RunInterrupted(
            **envelope("run.interrupted"),
            status="interrupted",
            reason="input_required",
        )
    elif incomplete_stream:
        yield RunFailed(
            **envelope("run.failed"),
            status="failed",
            error=ErrorInfo(
                code="incomplete_stream",
                message="ADK stream ended before message completion",
                source="adk",
                scope_id=root_scope_id,
            ),
        )
    else:
        yield RunCompleted(
            **envelope("run.completed"),
            status="completed",
            output_refs=output_refs,
        )


def _delegated_binding(agent, transfer_target):
    """Select the root's explicit handoff or static final delegate, never all children."""
    from google.adk.agents import SequentialAgent

    from ksadk.a2a.adk import A2ARemoteAgent

    if transfer_target:
        finder = getattr(agent, "find_agent", None)
        agent = finder(transfer_target) if callable(finder) else None
    else:
        while isinstance(agent, SequentialAgent) and agent.sub_agents:
            agent = agent.sub_agents[-1]
    return agent.remote_spec.binding_id if isinstance(agent, A2ARemoteAgent) else None


async def legacy_chunks(
    events: AsyncIterator[RuntimeEvent],
) -> AsyncGenerator[dict[str, Any], None]:
    """Project canonical item mutations to the existing dict chunk contract."""

    lanes: dict[str, dict[tuple[str, str], dict[str, str]]] = {
        "message": {},
        "reasoning": {},
    }
    usage: dict[str, int] = {}
    delegated_snapshots = {}

    async for event in events:
        if isinstance(event, ItemCompleted) and event.item_kind in {"message", "artifact"}:
            delegated_snapshots[(event.scope_id, event.item_id)] = {
                part.part_id: part.text
                for part in event.snapshot.parts
                if isinstance(part, TextContent)
            }
        if getattr(event, "parent_scope_id", None) and event.event_type.startswith("item."):
            # Legacy consumers cannot address child replacement. Only the root
            # terminal may select child text, and it emits that final once.
            continue
        if isinstance(event, (ItemUpdated, ItemCompleted)) and event.item_kind in lanes:
            lane = lanes[event.item_kind]
            key = (event.scope_id, event.item_id)
            key_existed = key in lane
            before = _flatten_lane(lane)
            explicit_replace = event.source.metadata.get("ksadk_output_snapshot") is True
            if explicit_replace:
                lane.clear()
            if isinstance(event, ItemUpdated):
                if not isinstance(event.update, TextContent):
                    continue
                parts = lane.setdefault(key, {})
                previous = parts.get(event.update.part_id, "")
                parts[event.update.part_id] = (
                    previous + event.update.text if event.op == "append" else event.update.text
                )
            else:
                lane[key] = {
                    part.part_id: part.text
                    for part in event.snapshot.parts
                    if isinstance(part, TextContent)
                }
            after = _flatten_lane(lane)
            if after == before:
                continue

            is_tail_append = (
                not explicit_replace
                and next(reversed(lane)) == key
                and (
                    (
                        isinstance(event, ItemUpdated)
                        and event.op == "append"
                        and next(reversed(lane[key])) == event.update.part_id
                    )
                    or (isinstance(event, ItemCompleted) and not key_existed)
                )
            )
            chunk: dict[str, Any] = {
                "type": "text" if event.item_kind == "message" else "thinking",
                "delta": (
                    event.update.text
                    if is_tail_append and isinstance(event, ItemUpdated)
                    else after[len(before) :]
                    if is_tail_append
                    else after
                ),
            }
            if not is_tail_append:
                chunk["replace"] = True
            yield chunk
            continue

        if isinstance(event, ItemCompleted):
            for part in event.snapshot.parts:
                if isinstance(part, ToolCallContent):
                    yield {
                        "type": "tool_call",
                        "tool_call_id": part.call_id,
                        "tool_name": part.name,
                        "tool_args": part.arguments,
                    }
                elif isinstance(part, ToolResultContent):
                    yield {
                        "type": "tool_result",
                        "tool_call_id": part.call_id,
                        "tool_name": event.source.metadata.get("tool_name", "tool"),
                        "tool_output": part.result,
                    }
            continue

        if isinstance(event, InteractionRequested):
            yield {
                "type": "interrupt",
                "interrupt_info": (
                    event.request.detail
                    if isinstance(event.request, ApprovalRequest)
                    else {"message": event.request.prompt}
                ),
                "session_id": event.source.metadata.get("session_id"),
            }
        elif isinstance(event, UsageReported):
            usage = {
                field: getattr(event, field)
                for field in (
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "cached_tokens",
                    "reasoning_tokens",
                )
            }
        elif isinstance(event, RunCanceled):
            yield {"type": "cancelled", "reason": event.reason}
        elif isinstance(event, RunFailed):
            raise RuntimeError(event.error.message or event.error.code)
        elif isinstance(event, RunCompleted):
            detailed_usage = event.source.metadata.get("usage")
            final: dict[str, Any] = {
                "type": "final",
                "output": _selected_text(
                    {**delegated_snapshots, **lanes["message"]}, event.output_refs
                ),
            }
            if isinstance(detailed_usage, dict):
                final["usage"] = detailed_usage
            elif usage:
                final["usage"] = usage
            if "last_usage" in event.source.metadata:
                final["metadata"] = {"last_usage": event.source.metadata["last_usage"]}
            yield final


def _flatten_lane(items: dict[tuple[str, str], dict[str, str]]) -> str:
    return "".join("".join(parts.values()) for parts in items.values())


def _selected_text(items: dict, refs: tuple) -> str:
    return "".join(
        items.get((ref.scope_id, ref.item_id), {}).get(ref.part_id, "")
        if ref.part_id
        else "".join(items.get((ref.scope_id, ref.item_id), {}).values())
        for ref in refs
    )


__all__ = ["canonical_events", "legacy_chunks"]
