"""AG-UI Agent duck seam backed exclusively by a KSADK RuntimeAdapter."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, AsyncIterator, Callable, Mapping, Optional, cast

from ag_ui.core import (
    ActivitySnapshotEvent,
    BaseEvent,
    Interrupt,
    ReasoningEndEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    ReasoningStartEvent,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    RunStartedEvent,
    StateSnapshotEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)

from ksadk.agui import _agent_helpers
from ksadk.conversations.runtime_metadata import (
    _update_session_metadata_after_assistant_turn,
    prime_session_metadata_for_user_turn,
)
from ksadk.events.canonical import (
    ApprovalResponse,
    ContentSnapshot,
    ContinuationCreated,
    InteractionRequested,
    InteractionResolved,
    ItemCompleted,
    ItemSnapshotReplaced,
    ItemStarted,
    ItemUpdated,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunStarted,
    RuntimeEvent,
    SourceRef,
)
from ksadk.events.content import (
    TextContent,
    ToolCallContent,
    ToolResultContent,
)
from ksadk.runtime.adapter import (
    CancelResult,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeLaunchContext,
    StartRequest,
)
from ksadk.runtime.executor import RuntimeExecutor

EventStoreFactory = Callable[[], Any]
SessionServiceFactory = Callable[[], Any]
logger = logging.getLogger(__name__)


def _split_a2ui_schema_context(context: list[Any]) -> tuple[Any, list[Any]]:
    """Call the optional toolkit without requiring its package to ship stubs."""
    module = import_module("ag_ui_a2ui_toolkit")
    splitter = getattr(module, "split_a2ui_schema_context", None)
    if not callable(splitter):
        raise RuntimeError("ag-ui-a2ui-toolkit does not expose split_a2ui_schema_context")
    result = splitter(context)
    if not isinstance(result, tuple) or len(result) != 2 or not isinstance(result[1], list):
        raise RuntimeError("ag-ui-a2ui-toolkit returned an invalid schema context split")
    return result[0], result[1]


@dataclass
class _PendingInterrupt:
    interrupt: Interrupt
    checkpoint_id: str


@dataclass
class _ThreadRun:
    handle: RunHandle
    active: bool = False
    interrupted: bool = False
    cancel_failed: bool = False
    pending: dict[str, _PendingInterrupt] = field(default_factory=dict)


@dataclass
class _SharedRuntime:
    executor: RuntimeExecutor
    launch_context: RuntimeLaunchContext
    event_store_factory: Optional[EventStoreFactory]
    session_service_factory: Optional[SessionServiceFactory]
    threads: dict[str, _ThreadRun] = field(default_factory=dict)
    consumed_resumes: dict[tuple[str, str], Any] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _WireState:
    thread_id: str
    run_id: str
    message_id: str
    reasoning_id: str
    text_open: bool = False
    reasoning_open: bool = False
    terminal: bool = False
    text_content: str = ""
    reasoning_content: str = ""


class KsadkAGUIAgent:
    """Implements the endpoint helper's pinned ``name/clone/run`` seam.

    The official ``ag-ui-langgraph==0.0.42`` helper clones this object per HTTP
    request and encodes the official event models yielded by :meth:`run`. The
    shared state contains only the app-owned RuntimeAdapter and handle registry;
    no native graph or runner-private attribute is exposed here.
    """

    def __init__(
        self,
        *,
        name: str,
        executor: RuntimeExecutor | None = None,
        launch_context: RuntimeLaunchContext | None = None,
        event_store_factory: Optional[EventStoreFactory] = None,
        session_service_factory: Optional[SessionServiceFactory] = None,
        _shared: Optional[_SharedRuntime] = None,
    ) -> None:
        self.name = name
        if _shared is not None:
            self._shared = _shared
        else:
            if executor is None or launch_context is None:
                raise ValueError("AG-UI requires RuntimeExecutor and RuntimeLaunchContext")
            self._shared = _SharedRuntime(
                executor,
                launch_context,
                event_store_factory,
                session_service_factory,
            )

    def clone(self) -> "KsadkAGUIAgent":
        return type(self)(name=self.name, _shared=self._shared)

    async def run(self, input: RunAgentInput) -> AsyncIterator[BaseEvent]:
        from ksadk.kernel import ingress as _kernel_ingress

        if _kernel_ingress.kernel_route_active():
            async for event in self._kernel_run(input):
                yield event
            return
        wire = _WireState(
            thread_id=input.thread_id,
            run_id=input.run_id,
            message_id=f"{input.run_id}:assistant",
            reasoning_id=f"{input.run_id}:reasoning",
        )
        yield RunStartedEvent(
            thread_id=input.thread_id,
            run_id=input.run_id,
            parent_run_id=input.parent_run_id,
            input=input,
        )

        run: Optional[_ThreadRun] = None
        try:
            run, duplicate = await self._resolve_run(input)
            if duplicate:
                yield RunFinishedEvent(
                    thread_id=input.thread_id,
                    run_id=input.run_id,
                    outcome=RunFinishedSuccessOutcome(),
                    result={"status": "already_resumed"},
                )
                return

            run.active = True
            async for runtime_event in self._shared.executor.stream(run.handle):
                persisted = await self._persist(
                    self._event_for_persistence(runtime_event, run),
                    session_id=input.thread_id,
                )
                async for event in self._project(persisted, wire, run):
                    yield event
            if not wire.terminal:
                async for event in self._finish_success(wire):
                    yield event
            if not run.interrupted:
                await self._update_session_metadata_after_assistant_turn(input, wire)
                await self._close_thread(input.thread_id, run)
        except (asyncio.CancelledError, GeneratorExit):
            if run is not None:
                await self._cancel_and_close(input.thread_id, run)
            raise
        except Exception:
            logger.exception(
                "AG-UI runtime execution failed for thread=%s run=%s",
                input.thread_id,
                input.run_id,
            )
            if run is not None:
                await self._close_thread(input.thread_id, run)
            if not wire.terminal:
                async for event in self._close_open_messages(wire):
                    yield event
                yield RunErrorEvent(
                    message="Runtime execution failed",
                    code="RUNTIME_ERROR",
                )
        finally:
            if run is not None and not run.cancel_failed:
                run.active = False

    async def _kernel_run(self, input: RunAgentInput) -> AsyncIterator[BaseEvent]:
        """kernel 路径（灰度 opt-in）：AG-UI run -> AgentControlCommand -> receipt。

        mutation 只走 kernel.submit；AG-UI 事件 shape 保留，cursor 源自同一
        Session seq（SessionEventSubscription.after_seq）。
        """
        from ksadk.kernel import ingress as _kernel_ingress

        yield RunStartedEvent(
            thread_id=input.thread_id,
            run_id=input.run_id,
            parent_run_id=input.parent_run_id,
            input=input,
        )
        message_id = f"{input.run_id}:assistant"
        text = ""
        try:
            trusted = _kernel_ingress.trusted_context(
                source_kind="agui",
                source_ref=input.run_id,
                session_id=input.thread_id,
                operations=("enqueue",),
                launch_context=self._shared.launch_context,
            )
            command = _kernel_ingress.map_agui_request(
                session_id=input.thread_id,
                idempotency_key=input.run_id,
                content=[
                    {"role": "user", "content": _agui_user_text(input)}
                ],
                run_id=input.run_id,
                trusted=trusted,
            )
            receipt = await _kernel_ingress.submit_command(
                command, permit=trusted.permit
            )
            if receipt.status not in ("accepted", "duplicate"):
                yield RunErrorEvent(
                    message=f"agent kernel rejected command: {receipt.status}",
                    code=receipt.status.upper(),
                )
                return
            text_open = False
            async for _seq, payload in _kernel_ingress.subscribe_projected(
                input.thread_id,
                trusted=trusted,
                after_seq=int(receipt.accepted_seq or 0),
                projector=_agui_envelope_payload,
            ):
                if payload is None:
                    continue
                kind, value = payload
                if kind == "delta":
                    if not text_open:
                        text_open = True
                        yield TextMessageStartEvent(message_id=message_id)
                    text += value
                    yield TextMessageContentEvent(message_id=message_id, delta=value)
                elif kind == "completed":
                    final = value or text
                    if final and not text_open:
                        text_open = True
                        yield TextMessageStartEvent(message_id=message_id)
                    if final.startswith(text) and len(final) > len(text):
                        yield TextMessageContentEvent(
                            message_id=message_id, delta=final[len(text):]
                        )
                    text = final
                    if text_open:
                        yield TextMessageEndEvent(message_id=message_id)
                    yield RunFinishedEvent(
                        thread_id=input.thread_id,
                        run_id=input.run_id,
                        outcome=RunFinishedSuccessOutcome(),
                        result={"output_text": text},
                    )
                    return
            if text_open:
                yield TextMessageEndEvent(message_id=message_id)
            yield RunFinishedEvent(
                thread_id=input.thread_id,
                run_id=input.run_id,
                outcome=RunFinishedSuccessOutcome(),
                result={"output_text": text},
            )
        except Exception:
            logger.exception(
                "AG-UI kernel ingress failed for thread=%s run=%s",
                input.thread_id,
                input.run_id,
            )
            yield RunErrorEvent(
                message="Agent kernel ingress failed",
                code="KERNEL_ERROR",
            )

    async def _resolve_run(self, input: RunAgentInput) -> tuple[_ThreadRun, bool]:
        async with self._shared.lock:
            if input.resume:
                return await self._resume_locked(input)

            current = self._shared.threads.get(input.thread_id)
            if current is not None and (current.active or current.interrupted):
                raise ValueError(f"thread {input.thread_id!r} already has an active run")

            forwarded = input.forwarded_props if isinstance(input.forwarded_props, dict) else {}
            tools = [tool.model_dump(by_alias=True) for tool in input.tools]
            a2ui_schema, regular_context = _split_a2ui_schema_context(input.context)
            ag_ui_state: dict[str, Any] = {
                "tools": tools,
                "context": [item.model_dump(by_alias=True) for item in regular_context],
            }
            if a2ui_schema is not None:
                ag_ui_state["a2ui_schema"] = a2ui_schema
            inject_a2ui_tool = forwarded.get("injectA2UITool", forwarded.get("inject_a2ui_tool"))
            if inject_a2ui_tool is not None:
                ag_ui_state["inject_a2ui_tool"] = inject_a2ui_tool
            request_config = dict(input.state) if isinstance(input.state, dict) else {}
            request_config.update(
                {
                    "ag-ui": ag_ui_state,
                    "copilotkit": {"actions": tools},
                }
            )
            request = StartRequest(
                input=self._latest_user_input(input),
                user_id=str(forwarded.get("userId") or forwarded.get("user_id") or "agui-user"),
                session_id=input.thread_id,
                agent_id=self.name,
                model=str(forwarded.get("model") or "") or None,
                config=request_config,
                metadata={"invocation_id": input.run_id, "transport": "ag-ui"},
            )
            handle = await self._shared.executor.start(
                self._shared.launch_context,
                request,
            )
            await self._persist_user_input(input, request, handle)
            run = _ThreadRun(handle=handle)
            self._shared.threads[input.thread_id] = run
            return run, False

    async def _resume_locked(self, input: RunAgentInput) -> tuple[_ThreadRun, bool]:
        entries = input.resume or []
        ids = [entry.interrupt_id for entry in entries]
        if len(ids) != len(set(ids)):
            raise ValueError("resume contains duplicate interruptId values")

        current = self._shared.threads.get(input.thread_id)
        if current is None or not current.interrupted:
            consumed_fingerprints = {
                entry.interrupt_id: self._resume_fingerprint(entry.status, entry.payload)
                for entry in entries
            }
            if consumed_fingerprints and all(
                self._shared.consumed_resumes.get((input.thread_id, interrupt_id)) == fingerprint
                for interrupt_id, fingerprint in consumed_fingerprints.items()
            ):
                return self._duplicate_run(input), True
            current, duplicate = await self._restore_durable_run(input, consumed_fingerprints)
            if duplicate:
                return self._duplicate_run(input), True
            if current is None:
                raise ValueError(f"thread {input.thread_id!r} has no resumable run")
            self._shared.threads[input.thread_id] = current

        pending_ids = set(current.pending)
        if set(ids) != pending_ids:
            missing = sorted(pending_ids - set(ids))
            unknown = sorted(set(ids) - pending_ids)
            raise ValueError(
                f"resume does not match pending interrupts; missing={missing}, unknown={unknown}"
            )

        checkpoint_ids = {pending.checkpoint_id for pending in current.pending.values()}
        if len(checkpoint_ids) != 1 or not next(iter(checkpoint_ids), ""):
            raise ValueError("pending interrupts do not share a resumable checkpoint")

        decisions: dict[str, Any] = {}
        fingerprints: dict[str, Any] = {}
        for entry in entries:
            payload = entry.payload if entry.status == "resolved" else {"type": "reject"}
            decisions[entry.interrupt_id] = payload
            fingerprints[entry.interrupt_id] = self._resume_fingerprint(entry.status, entry.payload)

        single_id = ids[0] if len(ids) == 1 else None
        payload_data = decisions[single_id] if single_id is not None else decisions
        reservation_created = await self._persist_resolved_approvals(input, current, entries)
        if not reservation_created:
            return self._duplicate_run(input), True
        for interrupt_id, fingerprint in fingerprints.items():
            self._shared.consumed_resumes[(input.thread_id, interrupt_id)] = fingerprint

        resumed = await self._shared.executor.resume(
            current.handle,
            ResumeTarget(kind="checkpoint_id", id=next(iter(checkpoint_ids))),
            ResumePayload(
                kind="approval_decision",
                call_id=single_id,
                data=payload_data,
            ),
        )
        current.handle = resumed
        current.interrupted = False
        current.pending.clear()
        return current, False

    async def _persist_resolved_approvals(
        self,
        input: RunAgentInput,
        run: _ThreadRun,
        entries: list[Any],
    ) -> bool:
        if self._shared.event_store_factory is None:
            return True
        reservation_created = True
        for index, entry in enumerate(entries):
            fingerprint = self._resume_fingerprint(entry.status, entry.payload)
            event_key = json.dumps(
                [input.thread_id, entry.interrupt_id],
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            )
            decision = self._approval_decision_for_audit(entry.status, entry.payload)
            event = InteractionResolved(
                schema_version=2,
                event_id=f"evt_agui_resume_{hashlib.sha256(event_key.encode()).hexdigest()}",
                seq=0,
                timestamp=time.time(),
                run_id=run.handle.run_id,
                scope_id=f"ksadk:{run.handle.run_id}",
                source=SourceRef(
                    framework="ksadk",
                    metadata={
                        "agent_id": str(run.handle.native_ref.get("agent_id") or self.name),
                        "user_id": str(run.handle.native_ref.get("user_id") or "agui-user"),
                        "session_id": input.thread_id,
                        "protocol": "ag-ui",
                        "resume_fingerprint": fingerprint,
                    },
                ),
                interaction_id=entry.interrupt_id,
                interaction_kind="approval",
                response=ApprovalResponse(
                    response_type="approval",
                    decision=(
                        decision if decision in ("approved", "rejected", "canceled") else "rejected"
                    ),
                    data={"call_id": entry.interrupt_id},
                ),
            )
            if index == 0:
                _persisted, reservation_created = await self._reserve(
                    event, session_id=input.thread_id
                )
                if not reservation_created:
                    return False
            else:
                await self._persist(event, session_id=input.thread_id)
        return reservation_created

    async def _project(
        self,
        event: RuntimeEvent,
        wire: _WireState,
        run: _ThreadRun,
    ) -> AsyncIterator[BaseEvent]:
        # ---- message (text) ----
        if isinstance(event, (ItemUpdated, ItemSnapshotReplaced)) and event.item_kind == "message":
            if not wire.text_open:
                wire.text_open = True
                yield TextMessageStartEvent(message_id=wire.message_id)
            if isinstance(event, ItemSnapshotReplaced):
                # atomic snapshot replace: close and reopen with full text
                wire.text_open = False
                wire.text_content = ""
                yield TextMessageEndEvent(message_id=wire.message_id)
                wire.text_open = True
                yield TextMessageStartEvent(message_id=wire.message_id)
                text = self._extract_text(event.snapshot)
            elif event.op == "replace":
                wire.text_open = False
                wire.text_content = ""
                yield TextMessageEndEvent(message_id=wire.message_id)
                wire.text_open = True
                yield TextMessageStartEvent(message_id=wire.message_id)
                text = event.update.text if isinstance(event.update, TextContent) else ""
            else:
                text = event.update.text if isinstance(event.update, TextContent) else ""
            if text:
                yield TextMessageContentEvent(message_id=wire.message_id, delta=text)
                wire.text_content += text
            return

        if isinstance(event, ItemCompleted) and event.item_kind == "message":
            if not wire.text_open:
                wire.text_open = True
                yield TextMessageStartEvent(message_id=wire.message_id)
            # ItemCompleted is the authoritative snapshot; do NOT re-append
            # the full text when deltas already covered the content.
            if not wire.text_content:
                text = self._extract_text(event.snapshot)
                if text:
                    yield TextMessageContentEvent(message_id=wire.message_id, delta=text)
                    wire.text_content += text
            wire.text_open = False
            yield TextMessageEndEvent(message_id=wire.message_id)
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
                yield ToolCallStartEvent(
                    tool_call_id=call_id,
                    tool_call_name=part.name,
                    parent_message_id=wire.message_id,
                )
                yield ToolCallArgsEvent(
                    tool_call_id=call_id,
                    delta=json.dumps(part.arguments, ensure_ascii=False, default=str),
                )
            return

        if isinstance(event, ItemCompleted) and event.item_kind == "tool_call":
            part = self._first_part(event.snapshot)
            call_id = part.call_id if isinstance(part, ToolCallContent) else "tool"
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
            run.pending[interrupt.id] = _PendingInterrupt(interrupt, checkpoint_id)
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
            async for finish_event in self._finish_success(wire):
                yield finish_event
            return

        if isinstance(event, RunCanceled):
            async for close_event in self._close_open_messages(wire):
                yield close_event
            wire.terminal = True
            yield RunErrorEvent(
                message=event.reason or "Runtime run was cancelled",
                code="CANCELLED",
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

    async def _persist(self, event: RuntimeEvent, *, session_id: str = "") -> RuntimeEvent:
        factory = self._shared.event_store_factory
        if factory is None:
            return event
        store = factory()
        sid = str(event.source.metadata.get("session_id") or session_id or "")
        return cast(RuntimeEvent, await store.append_one(sid, event))

    async def _reserve(
        self, event: RuntimeEvent, *, session_id: str = ""
    ) -> tuple[RuntimeEvent, bool]:
        factory = self._shared.event_store_factory
        if factory is None:
            return event, True
        store = factory()
        reserve = getattr(store, "reserve_once", None)
        if callable(reserve):
            persisted, created = await reserve(event)
            return cast(RuntimeEvent, persisted), bool(created)
        sid = str(event.source.metadata.get("session_id") or session_id or "")
        return cast(RuntimeEvent, await store.append_one(sid, event)), True

    async def _persist_user_input(
        self,
        input: RunAgentInput,
        request: StartRequest,
        handle: RunHandle,
    ) -> None:
        event_key = json.dumps([input.thread_id, input.run_id, "user"], ensure_ascii=False)
        await self._persist(
            RunStarted(
                schema_version=2,
                event_id=f"evt_agui_input_{hashlib.sha256(event_key.encode()).hexdigest()}",
                seq=0,
                timestamp=time.time(),
                run_id=input.run_id,
                scope_id=f"ksadk:{input.run_id}",
                source=SourceRef(
                    framework="ksadk",
                    metadata={
                        "agent_id": self.name,
                        "user_id": request.user_id,
                        "session_id": input.thread_id,
                        "input": self._json_safe(request.input),
                        "source": "ag-ui",
                        "runtime_type": handle.runtime_type,
                    },
                ),
                status="running",
            ),
            session_id=input.thread_id,
        )
        await self._prime_session_metadata_for_user_turn(
            session_id=input.thread_id,
            user_input=self._input_text(request.input),
        )

    async def _prime_session_metadata_for_user_turn(
        self,
        *,
        session_id: str,
        user_input: str,
    ) -> None:
        factory = self._shared.session_service_factory
        if factory is None or not user_input:
            return
        try:
            service = factory()
            session = await service.get_session(session_id)
            if session is not None:
                await prime_session_metadata_for_user_turn(
                    service=service,
                    session=session,
                    user_input=user_input,
                )
        except Exception:
            # Metadata enrichment must not make an otherwise valid agent run fail.
            logger.debug("failed to prime AG-UI session metadata", exc_info=True)

    async def _update_session_metadata_after_assistant_turn(
        self,
        input: RunAgentInput,
        wire: _WireState,
    ) -> None:
        factory = self._shared.session_service_factory
        if factory is None or not wire.text_content:
            return
        try:
            await _update_session_metadata_after_assistant_turn(
                service=factory(),
                session_id=input.thread_id,
                assistant_text=wire.text_content,
                model=None,
            )
        except Exception:
            logger.debug("failed to update AG-UI session metadata", exc_info=True)

    def _event_for_persistence(self, event: RuntimeEvent, run: _ThreadRun) -> RuntimeEvent:
        if isinstance(event, (InteractionRequested, InteractionResolved)):
            new_source = event.source.model_copy(
                update={"metadata": {**event.source.metadata, "protocol": "ag-ui"}}
            )
            return event.model_copy(update={"source": new_source})
        if not isinstance(event, RunInterrupted):
            return event
        new_source = event.source.model_copy(
            update={
                "metadata": {
                    **event.source.metadata,
                    "runtime_handle": self._durable_handle(run.handle),
                }
            }
        )
        return event.model_copy(update={"source": new_source})

    async def _restore_durable_run(
        self,
        input: RunAgentInput,
        fingerprints: Mapping[str, str],
    ) -> tuple[_ThreadRun | None, bool]:
        events = await self._durable_events(input.thread_id)
        if not events:
            return None, False
        resolved = {
            str(event.interaction_id): event
            for event in events
            if isinstance(event, InteractionResolved)
        }
        if fingerprints and all(interrupt_id in resolved for interrupt_id in fingerprints):
            for interrupt_id, fingerprint in fingerprints.items():
                persisted = str(
                    resolved[interrupt_id].source.metadata.get("resume_fingerprint") or ""
                )
                if persisted and persisted != fingerprint:
                    raise ValueError(f"interrupt {interrupt_id!r} was resolved differently")
            return None, True
        if any(interrupt_id in resolved for interrupt_id in fingerprints):
            raise ValueError("resume set is only partially resolved")

        interrupted = next(
            (event for event in reversed(events) if isinstance(event, RunInterrupted)),
            None,
        )
        if interrupted is None:
            return None, False
        raw_handle = interrupted.source.metadata.get("runtime_handle")
        if not isinstance(raw_handle, dict):
            return None, False
        handle = RunHandle.model_validate(raw_handle)
        requests = [
            event
            for event in events
            if isinstance(event, InteractionRequested)
            and event.run_id == interrupted.run_id
            and str(event.interaction_id) not in resolved
        ]
        pending: dict[str, _PendingInterrupt] = {}
        checkpoint_id = str(handle.native_ref.get("checkpoint_id") or "")
        if not checkpoint_id:
            known = handle.native_ref.get("known_checkpoint_ids") or []
            checkpoint_id = str(known[-1]) if known else ""
        for request in requests:
            interrupt = self._interrupt_from_interaction(request)
            pending[interrupt.id] = _PendingInterrupt(interrupt, checkpoint_id)
        if not pending:
            return None, False
        if not self._shared.executor.is_attached(handle):
            handle = await self._shared.executor.attach(
                self._shared.launch_context,
                handle,
            )
        return _ThreadRun(handle=handle, interrupted=True, pending=pending), False

    async def _durable_events(self, session_id: str) -> list[RuntimeEvent]:
        factory = self._shared.event_store_factory
        if factory is None:
            return []
        store = factory()
        list_events = getattr(store, "list", None)
        if not callable(list_events):
            return []
        return list(await list_events(session_id))

    async def _close_thread(self, thread_id: str, run: _ThreadRun) -> None:
        try:
            await self._shared.executor.close(run.handle)
        finally:
            async with self._shared.lock:
                if self._shared.threads.get(thread_id) is run:
                    self._shared.threads.pop(thread_id, None)

    async def _cancel_and_close(self, thread_id: str, run: _ThreadRun) -> CancelResult:
        result = await self._shared.executor.cancel(run.handle)
        if result in {
            CancelResult.INTERRUPTED_ACTIVE_TURN,
            CancelResult.PENDING_CANCEL_RECORDED,
            CancelResult.NOT_RUNNING,
        }:
            await self._close_thread(thread_id, run)
        elif result == CancelResult.FAILED:
            # Keep the handle blocked in the registry: cancellation was not
            # acknowledged, so accepting a replacement run would be dishonest.
            run.cancel_failed = True
            run.active = True
        return result

    @staticmethod
    def _approval_decision_for_audit(status: str, payload: Any) -> str:
        return _agent_helpers.approval_decision_for_audit(status, payload)

    @staticmethod
    def _durable_handle(handle: RunHandle) -> dict[str, Any]:
        return _agent_helpers.durable_handle(handle)

    @staticmethod
    def _interrupt_from_payload(payload: Mapping[str, Any]) -> Interrupt:
        return _agent_helpers.interrupt_from_payload(payload)

    @staticmethod
    def _interrupt_from_interaction(event: InteractionRequested) -> Interrupt:
        return _agent_helpers.interrupt_from_interaction(event)

    @staticmethod
    def _extract_text(snapshot: ContentSnapshot | None) -> str:
        return _agent_helpers.extract_text(snapshot)

    @staticmethod
    def _first_part(snapshot: ContentSnapshot | None) -> Any:
        return _agent_helpers.first_part(snapshot)

    @staticmethod
    def _a2ui_operations(
        event: ItemStarted | ItemUpdated | ItemCompleted,
        surface_id: str,
    ) -> list[dict[str, Any]]:
        return _agent_helpers.a2ui_operations(event, surface_id)

    @staticmethod
    def _duplicate_run(input: RunAgentInput) -> _ThreadRun:
        return _agent_helpers.duplicate_run(input)

    @staticmethod
    def _json_safe(value: Any) -> Any:
        return _agent_helpers.json_safe(value)

    @staticmethod
    def _latest_user_input(input: RunAgentInput) -> Any:
        return _agent_helpers.latest_user_input(input)

    @staticmethod
    def _input_text(value: Any) -> str:
        return _agent_helpers.input_text(value)

    @staticmethod
    def _approval_action(detail: Mapping[str, Any]) -> Mapping[str, Any]:
        return _agent_helpers.approval_action(detail)

    @staticmethod
    def _approval_metadata(
        detail: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return _agent_helpers.approval_metadata(detail, payload)

    @staticmethod
    def _approval_message(detail: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
        return _agent_helpers.approval_message(detail, payload)

    @staticmethod
    def _resume_fingerprint(status: str, payload: Any) -> Any:
        return _agent_helpers.resume_fingerprint(status, payload)


__all__ = ["KsadkAGUIAgent"]

def _agui_user_text(input: RunAgentInput) -> str:
    parts = []
    for message in input.messages or []:
        content = getattr(message, "content", "")
        if isinstance(content, str):
            parts.append(content)
        elif content is not None:
            parts.append(str(content))
    return "\n".join(parts)


def _agui_envelope_payload(envelope) -> tuple[str, str] | None:
    """Session envelope -> AG-UI 文本投影；cursor 仍用 envelope.seq。"""

    payload = envelope.payload or {}
    if envelope.event_type == "run.completed":
        return "completed", str(payload.get("output_text") or "")
    text = str(payload.get("delta") or payload.get("text") or "")
    if text:
        return "delta", text
    return None
