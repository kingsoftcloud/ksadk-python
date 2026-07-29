"""AG-UI Agent duck seam backed exclusively by a KSADK RuntimeAdapter."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
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

from ksadk.agui.a2ui_projection import project_a2ui_operations
from ksadk.conversations.runtime_metadata import (
    _update_session_metadata_after_assistant_turn,
    prime_session_metadata_for_user_turn,
)
from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.runtime.adapter import (
    CancelResult,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    StartRequest,
)

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
    adapter: RuntimeAdapter
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
        adapter: RuntimeAdapter,
        event_store_factory: Optional[EventStoreFactory] = None,
        session_service_factory: Optional[SessionServiceFactory] = None,
        _shared: Optional[_SharedRuntime] = None,
    ) -> None:
        self.name = name
        self._shared = _shared or _SharedRuntime(
            adapter,
            event_store_factory,
            session_service_factory,
        )

    def clone(self) -> "KsadkAGUIAgent":
        return type(self)(name=self.name, adapter=self._shared.adapter, _shared=self._shared)

    async def run(self, input: RunAgentInput) -> AsyncIterator[BaseEvent]:
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
            async for runtime_event in self._shared.adapter.stream(run.handle):
                persisted = await self._persist(self._event_for_persistence(runtime_event, run))
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
            handle = await self._shared.adapter.start(request)
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

        resumed = await self._shared.adapter.resume(
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
            event = RuntimeEvent.create(
                EventType.APPROVAL_RESOLVED,
                event_id=f"evt_agui_resume_{hashlib.sha256(event_key.encode()).hexdigest()}",
                agent_id=str(run.handle.native_ref.get("agent_id") or self.name),
                user_id=str(run.handle.native_ref.get("user_id") or "agui-user"),
                session_id=input.thread_id,
                invocation_id=run.handle.run_id,
                seq_id=0,
                payload={
                    "approval_id": entry.interrupt_id,
                    "call_id": entry.interrupt_id,
                    "decision": decision,
                    "resume_fingerprint": fingerprint,
                    "protocol": "ag-ui",
                },
            )
            if index == 0:
                _persisted, reservation_created = await self._reserve(event)
                if not reservation_created:
                    return False
            else:
                await self._persist(event)
        return reservation_created

    @staticmethod
    def _approval_decision_for_audit(status: str, payload: Any) -> str:
        """Persist a stable decision value while accepting official AG-UI envelopes."""

        if status != "resolved":
            return "rejected"
        if payload is True:
            return "approved"
        if isinstance(payload, Mapping):
            for key in ("approve", "approved"):
                if key in payload:
                    return "approved" if bool(payload[key]) else "rejected"
            for key in ("decision", "type"):
                if key in payload:
                    return KsadkAGUIAgent._approval_decision_for_audit(status, payload[key])
            return "rejected"
        if isinstance(payload, str) and payload.strip().lower() in {"approve", "approved"}:
            return "approved"
        return "rejected"

    async def _project(
        self,
        event: RuntimeEvent,
        wire: _WireState,
        run: _ThreadRun,
    ) -> AsyncIterator[BaseEvent]:
        payload = event.payload
        event_type = event.event_type

        if event_type in (EventType.TEXT_DELTA, EventType.TEXT_COMPLETED):
            if not wire.text_open:
                wire.text_open = True
                yield TextMessageStartEvent(message_id=wire.message_id)
            text = str(payload.get("text") or "")
            if event_type == EventType.TEXT_COMPLETED and wire.text_content:
                text = text[len(wire.text_content) :] if text.startswith(wire.text_content) else ""
            if text:
                yield TextMessageContentEvent(message_id=wire.message_id, delta=text)
                wire.text_content += text
            if event_type == EventType.TEXT_COMPLETED:
                wire.text_open = False
                yield TextMessageEndEvent(message_id=wire.message_id)
            return

        if event_type in (EventType.REASONING_DELTA, EventType.REASONING_COMPLETED):
            if not wire.reasoning_open:
                wire.reasoning_open = True
                yield ReasoningStartEvent(message_id=wire.reasoning_id)
                yield ReasoningMessageStartEvent(message_id=wire.reasoning_id, role="reasoning")
            text = str(payload.get("text") or "")
            if text:
                yield ReasoningMessageContentEvent(message_id=wire.reasoning_id, delta=text)
            if event_type == EventType.REASONING_COMPLETED:
                wire.reasoning_open = False
                yield ReasoningMessageEndEvent(message_id=wire.reasoning_id)
                yield ReasoningEndEvent(message_id=wire.reasoning_id)
            return

        if event_type == EventType.TOOL_CALL_BEGIN:
            call_id = str(payload.get("call_id") or "tool")
            yield ToolCallStartEvent(
                tool_call_id=call_id,
                tool_call_name=str(payload.get("name") or "tool"),
                parent_message_id=wire.message_id,
            )
            if "args" in payload:
                yield ToolCallArgsEvent(
                    tool_call_id=call_id,
                    delta=json.dumps(payload.get("args"), ensure_ascii=False, default=str),
                )
            return

        if event_type == EventType.TOOL_CALL_END:
            call_id = str(payload.get("call_id") or "tool")
            content = (
                payload.get("result") if payload.get("error") is None else payload.get("error")
            )
            # AG-UI closes the active call at TOOL_CALL_END.  Sending a result
            # first makes official clients discard the call, then reject this
            # end event as orphaned.
            yield ToolCallEndEvent(tool_call_id=call_id)
            yield ToolCallResultEvent(
                message_id=f"{wire.run_id}:tool:{call_id}",
                tool_call_id=call_id,
                content=json.dumps(content, ensure_ascii=False, default=str),
                role="tool",
            )
            return

        if event_type == EventType.CHECKPOINT_CREATED:
            yield StateSnapshotEvent(snapshot={"checkpoint": copy.deepcopy(payload)})
            return

        if event_type == EventType.APPROVAL_REQUESTED:
            interrupt_id = str(payload.get("approval_id") or payload.get("call_id") or "")
            checkpoint_id = str(run.handle.native_ref.get("checkpoint_id") or "")
            if not checkpoint_id:
                known = run.handle.native_ref.get("known_checkpoint_ids") or []
                checkpoint_id = str(known[-1]) if known else ""
            raw_detail = payload.get("detail")
            detail: dict[str, Any] = raw_detail if isinstance(raw_detail, dict) else {}
            interrupt = Interrupt(
                id=interrupt_id,
                reason=str(detail.get("reason") or payload.get("kind") or "approval"),
                message=self._approval_message(detail, payload),
                tool_call_id=str(payload.get("call_id") or "") or None,
                response_schema=detail.get("response_schema"),
                metadata=self._approval_metadata(detail, payload),
            )
            run.pending[interrupt_id] = _PendingInterrupt(interrupt, checkpoint_id)
            return

        if event_type in {
            EventType.A2UI_SURFACE_BEGIN,
            EventType.A2UI_SURFACE_UPDATE,
            EventType.A2UI_SURFACE_END,
        }:
            operations = project_a2ui_operations(event_type, payload)
            if operations:
                surface_id = str(payload.get("surface_id") or payload.get("surfaceId") or "")
                yield ActivitySnapshotEvent(
                    message_id=f"{wire.run_id}:a2ui:{surface_id}",
                    activity_type="a2ui-surface",
                    # The client renderer addresses a surface by this value.
                    # ``message_id`` is a transport id, not the A2UI surface id.
                    content={
                        "surfaceId": surface_id,
                        "a2ui_operations": operations,
                    },
                    replace=True,
                )
            return

        if event_type == EventType.RUN_INTERRUPTED:
            async for close_event in self._close_open_messages(wire):
                yield close_event
            if not run.pending:
                raise ValueError("runtime interrupted without a pending approval")
            checkpoint_id = str(run.handle.native_ref.get("checkpoint_id") or "")
            if not checkpoint_id:
                known = run.handle.native_ref.get("known_checkpoint_ids") or []
                checkpoint_id = str(known[-1]) if known else ""
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

        if event_type == EventType.RUN_COMPLETED:
            async for finish_event in self._finish_success(wire):
                yield finish_event
            return

        if event_type in (EventType.RUN_FAILED, EventType.RUN_CANCELED):
            async for close_event in self._close_open_messages(wire):
                yield close_event
            wire.terminal = True
            yield RunErrorEvent(
                message=(
                    "Runtime run was cancelled"
                    if event_type == EventType.RUN_CANCELED
                    else "Runtime execution failed"
                ),
                code=("CANCELLED" if event_type == EventType.RUN_CANCELED else "RUNTIME_ERROR"),
            )

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

    async def _persist(self, event: RuntimeEvent) -> RuntimeEvent:
        factory = self._shared.event_store_factory
        if factory is None:
            return event
        store = factory()
        return cast(RuntimeEvent, await store.append_one(event))

    async def _reserve(self, event: RuntimeEvent) -> tuple[RuntimeEvent, bool]:
        factory = self._shared.event_store_factory
        if factory is None:
            return event, True
        store = factory()
        reserve = getattr(store, "reserve_once", None)
        if callable(reserve):
            persisted, created = await reserve(event)
            return cast(RuntimeEvent, persisted), bool(created)
        return cast(RuntimeEvent, await store.append_one(event)), True

    async def _persist_user_input(
        self,
        input: RunAgentInput,
        request: StartRequest,
        handle: RunHandle,
    ) -> None:
        event_key = json.dumps([input.thread_id, input.run_id, "user"], ensure_ascii=False)
        await self._persist(
            RuntimeEvent.create(
                EventType.RUN_STARTED,
                event_id=f"evt_agui_input_{hashlib.sha256(event_key.encode()).hexdigest()}",
                agent_id=self.name,
                user_id=request.user_id,
                session_id=input.thread_id,
                invocation_id=input.run_id,
                seq_id=0,
                payload={
                    "status": "in_progress",
                    "input": self._json_safe(request.input),
                    "source": "ag-ui",
                    "runtime_type": handle.runtime_type,
                },
            )
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
        if event.event_type in {
            EventType.APPROVAL_REQUESTED,
            EventType.APPROVAL_RESOLVED,
        }:
            return event.model_copy(update={"payload": {**event.payload, "protocol": "ag-ui"}})
        if event.event_type != EventType.RUN_INTERRUPTED:
            return event
        return event.model_copy(
            update={
                "payload": {
                    **event.payload,
                    "runtime_handle": self._durable_handle(run.handle),
                }
            }
        )

    async def _restore_durable_run(
        self,
        input: RunAgentInput,
        fingerprints: Mapping[str, str],
    ) -> tuple[_ThreadRun | None, bool]:
        events = await self._durable_events(input.thread_id)
        if not events:
            return None, False
        resolved = {
            str(event.payload.get("approval_id") or event.payload.get("call_id") or ""): event
            for event in events
            if event.event_type == EventType.APPROVAL_RESOLVED
        }
        if fingerprints and all(interrupt_id in resolved for interrupt_id in fingerprints):
            for interrupt_id, fingerprint in fingerprints.items():
                persisted = str(resolved[interrupt_id].payload.get("resume_fingerprint") or "")
                if persisted and persisted != fingerprint:
                    raise ValueError(f"interrupt {interrupt_id!r} was resolved differently")
            return None, True
        if any(interrupt_id in resolved for interrupt_id in fingerprints):
            raise ValueError("resume set is only partially resolved")

        interrupted = next(
            (event for event in reversed(events) if event.event_type == EventType.RUN_INTERRUPTED),
            None,
        )
        if interrupted is None:
            return None, False
        raw_handle = interrupted.payload.get("runtime_handle")
        if not isinstance(raw_handle, dict):
            return None, False
        handle = RunHandle.model_validate(raw_handle)
        requests = [
            event
            for event in events
            if event.invocation_id == interrupted.invocation_id
            and event.event_type == EventType.APPROVAL_REQUESTED
            and str(event.payload.get("approval_id") or event.payload.get("call_id") or "")
            not in resolved
        ]
        pending: dict[str, _PendingInterrupt] = {}
        checkpoint_id = str(handle.native_ref.get("checkpoint_id") or "")
        if not checkpoint_id:
            known = handle.native_ref.get("known_checkpoint_ids") or []
            checkpoint_id = str(known[-1]) if known else ""
        for request in requests:
            interrupt = self._interrupt_from_payload(request.payload)
            pending[interrupt.id] = _PendingInterrupt(interrupt, checkpoint_id)
        if not pending:
            return None, False
        if not self._shared.adapter.is_handle_attached(handle):
            handle = await self._shared.adapter.attach(handle)
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

    @staticmethod
    def _durable_handle(handle: RunHandle) -> dict[str, Any]:
        allowed = {
            "agent_id",
            "user_id",
            "checkpoint_id",
            "known_checkpoint_ids",
            "pending_approval_ids",
            "framework_ref",
            "thread_id",
            "checkpoint_ns",
            "resume_thread_id",
        }
        native_ref = {key: value for key, value in handle.native_ref.items() if key in allowed}
        return {
            "run_id": handle.run_id,
            "session_id": handle.session_id,
            "runtime_type": handle.runtime_type,
            "native_ref": KsadkAGUIAgent._json_safe(native_ref),
        }

    @staticmethod
    def _interrupt_from_payload(payload: Mapping[str, Any]) -> Interrupt:
        interrupt_id = str(payload.get("approval_id") or payload.get("call_id") or "")
        raw_detail = payload.get("detail")
        detail = raw_detail if isinstance(raw_detail, dict) else {}
        return Interrupt(
            id=interrupt_id,
            reason=str(detail.get("reason") or payload.get("kind") or "approval"),
            message=KsadkAGUIAgent._approval_message(detail, payload),
            tool_call_id=str(payload.get("call_id") or "") or None,
            response_schema=detail.get("response_schema"),
            metadata=KsadkAGUIAgent._approval_metadata(detail, payload),
        )

    @staticmethod
    def _duplicate_run(input: RunAgentInput) -> _ThreadRun:
        return _ThreadRun(
            handle=RunHandle(
                run_id=input.run_id,
                session_id=input.thread_id,
                runtime_type="ag-ui-duplicate",
            )
        )

    @staticmethod
    def _json_safe(value: Any) -> Any:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))

    async def _close_thread(self, thread_id: str, run: _ThreadRun) -> None:
        try:
            await self._shared.adapter.close(run.handle)
        finally:
            async with self._shared.lock:
                if self._shared.threads.get(thread_id) is run:
                    self._shared.threads.pop(thread_id, None)

    async def _cancel_and_close(self, thread_id: str, run: _ThreadRun) -> CancelResult:
        result = await self._shared.adapter.cancel(run.handle)
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
    def _latest_user_input(input: RunAgentInput) -> Any:
        for message in reversed(input.messages):
            if getattr(message, "role", None) == "user":
                return getattr(message, "content", "")
        return ""

    @staticmethod
    def _input_text(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, Mapping):
            return str(value.get("text") or value.get("content") or "").strip()
        return str(value or "").strip()

    @staticmethod
    def _approval_action(detail: Mapping[str, Any]) -> Mapping[str, Any]:
        nested_request = detail.get("approval_requests")
        actions = (
            nested_request.get("action_requests")
            if isinstance(nested_request, Mapping)
            else detail.get("action_requests")
        )
        if isinstance(actions, list):
            for action in actions:
                if isinstance(action, Mapping):
                    return action
        return {}

    @staticmethod
    def _approval_metadata(
        detail: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        action = KsadkAGUIAgent._approval_action(detail)
        arguments = (
            action.get("args")
            or action.get("arguments")
            or detail.get("arguments")
            or detail.get("args")
            or payload.get("args")
        )
        metadata: dict[str, Any] = {
            "tool_name": str(
                action.get("name")
                or detail.get("tool_name")
                or payload.get("name")
                or payload.get("kind")
                or "approval"
            ),
            "arguments": arguments if arguments is not None else {},
        }
        approval_level = (
            action.get("approval_level")
            or detail.get("approval_level")
            or payload.get("approval_level")
        )
        if approval_level:
            metadata["approval_level"] = str(approval_level)
        return metadata

    @staticmethod
    def _approval_message(detail: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
        action = KsadkAGUIAgent._approval_action(detail)
        return str(
            action.get("description")
            or detail.get("message")
            or payload.get("message")
            or "Approval required"
        )

    @staticmethod
    def _resume_fingerprint(status: str, payload: Any) -> Any:
        return json.dumps([status, payload], sort_keys=True, ensure_ascii=False, default=str)


__all__ = ["KsadkAGUIAgent"]
