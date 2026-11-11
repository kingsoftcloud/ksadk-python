"""AG-UI Agent duck seam backed exclusively by a KSADK RuntimeAdapter."""

from __future__ import annotations

import copy
import json
from typing import AsyncIterator

from ag_ui.core import (
    ActivitySnapshotEvent,
    BaseEvent,
    CustomEvent,
    ReasoningEndEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    ReasoningStartEvent,
    RunErrorEvent,
    RunFinishedCancelledOutcome,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    StateSnapshotEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)

from ksadk.agui._agent_types import _PendingInterrupt, _ThreadRun, _WireState
from ksadk.agui.config import AGUI_TEXT_MESSAGE_SNAPSHOT_NAME
from ksadk.events.canonical import (
    ContinuationCreated,
    InteractionRequested,
    ItemCompleted,
    ItemFailed,
    ItemSnapshotReplaced,
    ItemStarted,
    ItemUpdated,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RuntimeEvent,
)
from ksadk.events.content import (
    TextContent,
    ToolCallContent,
    ToolResultContent,
)


class AGUIProjectionMixin:
    async def _project(
        self,
        event: RuntimeEvent,
        wire: _WireState,
        run: _ThreadRun,
    ) -> AsyncIterator[BaseEvent]:
        if event.source.metadata.get(
            "conversation_visibility", event.source.metadata.get("visibility")
        ) in {"internal", "hidden"}:
            return
        # ---- message (text) ----
        if (
            isinstance(event, (ItemUpdated, ItemSnapshotReplaced, ItemCompleted, ItemFailed))
            and event.item_kind == "message"
        ):
            async for projected in self._project_message_text(event, wire):
                yield projected
            return

        if isinstance(event, ItemStarted) and event.item_kind == "message":
            # ItemStarted for messages is a tracking signal; the text message
            # opens lazily on the first content delta or completed snapshot.
            return

        # ---- reasoning ----
        if (
            isinstance(event, (ItemUpdated, ItemSnapshotReplaced))
            and event.item_kind == "reasoning"
        ):
            if not wire.reasoning_open:
                wire.reasoning_open = True
                yield ReasoningStartEvent(message_id=wire.reasoning_id)
                yield ReasoningMessageStartEvent(message_id=wire.reasoning_id, role="reasoning")
            if isinstance(event, ItemSnapshotReplaced):
                wire.reasoning_content = ""
                text = self._extract_text(event.snapshot)
            else:
                text = event.update.text if isinstance(event.update, TextContent) else ""
            if text:
                yield ReasoningMessageContentEvent(message_id=wire.reasoning_id, delta=text)
                wire.reasoning_content += text
            return

        if isinstance(event, ItemCompleted) and event.item_kind == "reasoning":
            if not wire.reasoning_open:
                wire.reasoning_open = True
                yield ReasoningStartEvent(message_id=wire.reasoning_id)
                yield ReasoningMessageStartEvent(message_id=wire.reasoning_id, role="reasoning")
            if not wire.reasoning_content:
                text = self._extract_text(event.snapshot)
                if text:
                    yield ReasoningMessageContentEvent(message_id=wire.reasoning_id, delta=text)
                    wire.reasoning_content += text
            wire.reasoning_open = False
            yield ReasoningMessageEndEvent(message_id=wire.reasoning_id)
            yield ReasoningEndEvent(message_id=wire.reasoning_id)
            return

        if isinstance(event, ItemStarted) and event.item_kind == "reasoning":
            return

        # ---- tool call ----
        if isinstance(event, ItemStarted) and event.item_kind == "tool_call":
            part = self._first_part(event.initial)
            if isinstance(part, ToolCallContent):
                call_id = part.call_id
                if call_id not in wire.tool_calls_started:
                    wire.tool_calls_started.add(call_id)
                    yield ToolCallStartEvent(
                        tool_call_id=call_id,
                        tool_call_name=part.name,
                        parent_message_id=wire.message_id,
                    )
                # Arguments may arrive after an empty canonical ItemStarted.
                # Send their authoritative completed value once; AG-UI args
                # append deltas and cannot replace an earlier JSON snapshot.
            return

        if isinstance(event, ItemCompleted) and event.item_kind == "tool_call":
            part = self._first_part(event.snapshot)
            if not isinstance(part, ToolCallContent):
                return
            call_id = part.call_id
            if call_id in wire.tool_calls_completed:
                return
            if call_id not in wire.tool_calls_started:
                wire.tool_calls_started.add(call_id)
                yield ToolCallStartEvent(
                    tool_call_id=call_id, tool_call_name=part.name,
                    parent_message_id=wire.message_id,
                )
            yield ToolCallArgsEvent(
                tool_call_id=call_id,
                delta=json.dumps(part.arguments, ensure_ascii=False, default=str),
            )
            wire.tool_calls_completed.add(call_id)
            yield ToolCallEndEvent(tool_call_id=call_id)
            return

        if isinstance(event, ItemCompleted) and event.item_kind == "tool_result":
            part = self._first_part(event.snapshot)
            if isinstance(part, ToolResultContent):
                call_id = part.call_id
                content = part.result
                yield ToolCallResultEvent(
                    message_id=f"{wire.run_id}:tool:{call_id}",
                    tool_call_id=call_id,
                    content=json.dumps(content, ensure_ascii=False, default=str),
                    role="tool",
                )
            return

        if isinstance(event, ItemStarted) and event.item_kind == "tool_result":
            return

        # ---- A2UI surface (item_kind="data" + source.protocol="a2ui") ----
        if (
            isinstance(event, (ItemStarted, ItemUpdated, ItemCompleted))
            and event.item_kind == "data"
            and event.source.protocol == "a2ui"
        ):
            surface_id = str(event.source.metadata.get("surface_id") or "")
            operations = self._a2ui_operations(event, surface_id)
            if operations:
                yield ActivitySnapshotEvent(
                    message_id=f"{wire.run_id}:a2ui:{surface_id}",
                    activity_type="a2ui-surface",
                    content={
                        "surfaceId": surface_id,
                        "a2ui_operations": operations,
                        "batchId": (event.item_id if event.source.metadata.get("operation_batch")
                                    else event.event_id),
                        **({"interactionId": event.source.metadata["interaction_id"]}
                           if event.source.metadata.get("interaction_id") else {}),
                    },
                    replace=True,
                )
            return

        # ---- checkpoint ----
        if isinstance(event, ContinuationCreated):
            # Track checkpoint id in handle native_ref for downstream approval
            # resolution (pending interrupts need a resumable checkpoint_id).
            ckpt_id = str(event.ref.get("checkpoint_id") or "")
            if ckpt_id:
                run.handle.native_ref["checkpoint_id"] = ckpt_id
                known = run.handle.native_ref.setdefault("known_checkpoint_ids", [])
                if ckpt_id not in known:
                    known.append(ckpt_id)
            yield StateSnapshotEvent(snapshot={"checkpoint": copy.deepcopy(event.ref)})
            return

        # ---- approval ----
        if isinstance(event, InteractionRequested):
            interrupt = self._interrupt_from_interaction(event)
            checkpoint_id = str(run.handle.native_ref.get("checkpoint_id") or "")
            if not checkpoint_id:
                known = run.handle.native_ref.get("known_checkpoint_ids") or []
                checkpoint_id = str(known[-1]) if known else ""
            run.pending[interrupt.id] = _PendingInterrupt(
                interrupt, checkpoint_id, event.interaction_kind
            )
            return

        # ---- run lifecycle ----
        if isinstance(event, RunInterrupted):
            async for close_event in self._close_open_messages(wire):
                yield close_event
            if not run.pending:
                raise ValueError("runtime interrupted without a pending approval")
            # If checkpoint_id wasn't set by ContinuationCreated (e.g. langgraph
            # canonical stream without checkpoint_ref on first run), try to
            # extract it from the RunInterrupted event's continuation_id or
            # the executor's checkpoint descriptor.
            if event.continuation_id:
                checkpoint_id = str(event.continuation_id)
            else:
                checkpoint_id = str(run.handle.native_ref.get("checkpoint_id") or "")
            if not checkpoint_id:
                known = run.handle.native_ref.get("known_checkpoint_ids") or []
                checkpoint_id = str(known[-1]) if known else ""
            if not checkpoint_id:
                # Last resort: query the executor for the native checkpoint.
                try:
                    descriptor = await self._shared.executor.checkpoint(run.handle)
                    checkpoint_id = str(descriptor.checkpoint_id or "")
                    if checkpoint_id:
                        run.handle.native_ref["checkpoint_id"] = checkpoint_id
                        known = run.handle.native_ref.setdefault("known_checkpoint_ids", [])
                        if checkpoint_id not in known:
                            known.append(checkpoint_id)
                except Exception:
                    pass
            if checkpoint_id:
                for pending in run.pending.values():
                    if not pending.checkpoint_id:
                        pending.checkpoint_id = checkpoint_id
            run.interrupted = True
            wire.terminal = True
            yield RunFinishedEvent(
                thread_id=wire.thread_id,
                run_id=wire.run_id,
                outcome=RunFinishedInterruptOutcome(
                    interrupts=[pending.interrupt for pending in run.pending.values()]
                ),
            )
            return

        if isinstance(event, RunCompleted):
            if event.output_refs:
                text = "".join(
                    text
                    for ref in event.output_refs
                    for part_id, text in wire.text_items.get(
                        (ref.scope_id, ref.item_id), {}
                    ).items()
                    if ref.part_id is None or ref.part_id == part_id
                )
                if text != wire.text_content:
                    yield CustomEvent(name=AGUI_TEXT_MESSAGE_SNAPSHOT_NAME, value={
                        "version": 1,
                        "threadId": wire.thread_id,
                        "runId": wire.run_id,
                        "messageId": wire.message_id,
                        "content": text,
                    })
                    wire.text_content = text
            async for finish_event in self._finish_success(wire):
                yield finish_event
            return

        if isinstance(event, RunCanceled):
            async for close_event in self._close_open_messages(wire):
                yield close_event
            wire.terminal = True
            yield RunFinishedEvent(
                thread_id=wire.thread_id,
                run_id=wire.run_id,
                outcome=RunFinishedCancelledOutcome(),
            )
            return

        if isinstance(event, RunFailed):
            async for close_event in self._close_open_messages(wire):
                yield close_event
            wire.terminal = True
            yield RunErrorEvent(
                message=event.error.message or "Runtime execution failed",
                code="RUNTIME_ERROR",
            )
            return

    @staticmethod
    async def _project_message_text(
        event: ItemUpdated | ItemSnapshotReplaced | ItemCompleted | ItemFailed,
        wire: _WireState,
    ) -> AsyncIterator[BaseEvent]:
        item_key = (event.scope_id, event.item_id)
        if isinstance(event, ItemFailed):
            wire.text_items.pop(item_key, None)
        elif isinstance(event, ItemUpdated):
            part = event.update
            if not isinstance(part, TextContent):
                return
            parts = wire.text_items.setdefault(item_key, {})
            parts[part.part_id] = (
                parts.get(part.part_id, "") + part.text if event.op == "append" else part.text
            )
        else:
            # A snapshot replaces this item's complete ordered part set. Keep
            # other items, but remove text parts absent from this snapshot.
            wire.text_items[item_key] = {
                part.part_id: part.text for part in event.snapshot.parts
                if isinstance(part, TextContent)
            }
        text = "".join(text for parts in wire.text_items.values() for text in parts.values())
        previous = wire.text_content
        if text != previous:
            if text.startswith(previous):
                if not wire.text_open:
                    wire.text_open = wire.text_started = True
                    yield TextMessageStartEvent(message_id=wire.message_id)
                yield TextMessageContentEvent(message_id=wire.message_id, delta=text[len(previous):])
            else:
                # A single-message replacement must not masquerade as a full
                # MESSAGES_SNAPSHOT: that would delete unrelated client history.
                yield CustomEvent(name=AGUI_TEXT_MESSAGE_SNAPSHOT_NAME, value={
                    "version": 1,
                    "threadId": wire.thread_id,
                    "runId": wire.run_id,
                    "messageId": wire.message_id,
                    "content": text,
                })
            wire.text_content = text
        if isinstance(event, ItemCompleted):
            if not wire.text_started:
                wire.text_started = True
                yield TextMessageStartEvent(message_id=wire.message_id)
                yield TextMessageEndEvent(message_id=wire.message_id)
            elif wire.text_open:
                wire.text_open = False
                yield TextMessageEndEvent(message_id=wire.message_id)

    async def _finish_success(self, wire: _WireState) -> AsyncIterator[BaseEvent]:
        async for event in self._close_open_messages(wire):
            yield event
        if not wire.terminal:
            wire.terminal = True
            yield RunFinishedEvent(
                thread_id=wire.thread_id,
                run_id=wire.run_id,
                outcome=RunFinishedSuccessOutcome(),
            )

    @staticmethod
    async def _close_open_messages(wire: _WireState) -> AsyncIterator[BaseEvent]:
        if wire.reasoning_open:
            wire.reasoning_open = False
            yield ReasoningMessageEndEvent(message_id=wire.reasoning_id)
            yield ReasoningEndEvent(message_id=wire.reasoning_id)
        if wire.text_open:
            wire.text_open = False
            yield TextMessageEndEvent(message_id=wire.message_id)
