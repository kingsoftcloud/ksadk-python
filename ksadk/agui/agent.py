"""AG-UI Agent duck seam backed exclusively by a KSADK RuntimeAdapter."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from importlib import import_module
from typing import Any, AsyncIterator, Callable, Mapping, Optional, cast

from ag_ui.core import (
    BaseEvent,
    Interrupt,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedCancelledOutcome,
    RunFinishedEvent,
    RunFinishedSuccessOutcome,
    RunStartedEvent,
)

from ksadk.agui import _agent_helpers
from ksadk.agui._agent_kernel import AGUIKernelMixin
from ksadk.agui._agent_projection import AGUIProjectionMixin
from ksadk.agui._agent_resume import AGUIResumeMixin, AguiRootNullUnsupported
from ksadk.agui._agent_types import _PendingInterrupt, _SharedRuntime, _ThreadRun, _WireState
from ksadk.conversations.runtime_metadata import (
    _update_session_metadata_after_assistant_turn,
    prime_session_metadata_for_user_turn,
)
from ksadk.events.canonical import (
    ContentSnapshot,
    InteractionRequested,
    InteractionResolved,
    ItemCompleted,
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
from ksadk.runtime.adapter import (
    CancelResult,
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


class KsadkAGUIAgent(AGUIKernelMixin, AGUIResumeMixin, AGUIProjectionMixin):
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
        local_kernel_provider=None,
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
                local_kernel_provider=local_kernel_provider,
            )

    def clone(self) -> "KsadkAGUIAgent":
        return type(self)(name=self.name, _shared=self._shared)

    async def run(self, input: RunAgentInput) -> AsyncIterator[BaseEvent]:
        from ksadk.agui.config import AGUI_PROTOCOL_VERSION
        from ksadk.kernel import ingress as _kernel_ingress

        if input.protocol_version not in {None, AGUI_PROTOCOL_VERSION, "1.0.0"}:
            yield RunErrorEvent(
                message="Unsupported AG-UI protocol version", code="UNSUPPORTED_PROTOCOL_VERSION"
            )
            return

        if (
            not _kernel_ingress.kernel_route_active()
            and self._shared.local_kernel_provider is not None
        ):
            await self._shared.local_kernel_provider()
        use_kernel = (
            _kernel_ingress.kernel_route_active()
            or _kernel_ingress.interaction_route_active(launch_context=self._shared.launch_context)
        )
        if use_kernel and input.resume and not _kernel_ingress.kernel_route_active():
            # Legacy pending approvals keep their existing restoration path.
            use_kernel = await self._kernel_owns_resume(input)
        if use_kernel:
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
            protocol_version=AGUI_PROTOCOL_VERSION,
            parent_run_id=input.parent_run_id,
            input=input,
        )

        run: Optional[_ThreadRun] = None
        try:
            from ksadk.interaction.schema import InputValidationError

            try:
                run, duplicate = await self._resolve_run(input)
            except InputValidationError:
                yield RunErrorEvent(
                    message="Response does not satisfy the input schema",
                    code="INPUT_RESPONSE_INVALID",
                )
                return
            except AguiRootNullUnsupported:
                yield RunErrorEvent(
                    message="Use Interaction/v1 to submit a root null answer",
                    code="INPUT_TRANSPORT_UNSUPPORTED",
                )
                return
            if duplicate:
                yield RunFinishedEvent(
                    thread_id=input.thread_id,
                    run_id=input.run_id,
                    outcome=RunFinishedSuccessOutcome(),
                    result={"status": "already_resumed"},
                )
                return

            if run.cancelled:
                yield RunFinishedEvent(
                    thread_id=input.thread_id,
                    run_id=input.run_id,
                    outcome=RunFinishedCancelledOutcome(),
                    result={"status": "cancelled"},
                )
                return

            run.active = True
            async for runtime_event in self._shared.executor.stream(run.handle):
                from ksadk.interaction.native_events import NativeSubmissionProgress

                if isinstance(runtime_event, NativeSubmissionProgress):
                    await self._record_native_resume_outcomes(input, run, runtime_event)
                    continue
                if runtime_event.event_type == "run.failed" and run.native_submissions:
                    run.interrupted = True
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

    @staticmethod
    def _request_config(input: RunAgentInput) -> dict[str, Any]:
        forwarded = input.forwarded_props if isinstance(input.forwarded_props, dict) else {}
        tools = [tool.model_dump(by_alias=True) for tool in (input.tools or [])]
        a2ui_schema, regular_context = _split_a2ui_schema_context(input.context or [])
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
        return request_config

    async def _resolve_run(self, input: RunAgentInput) -> tuple[_ThreadRun, bool]:
        async with self._shared.lock:
            if input.resume:
                return await self._resume_locked(input)

            current = self._shared.threads.get(input.thread_id)
            if current is not None and (current.active or current.interrupted):
                raise ValueError(f"thread {input.thread_id!r} already has an active run")

            forwarded = input.forwarded_props if isinstance(input.forwarded_props, dict) else {}
            request_config = self._request_config(input)
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
            from ksadk.interaction.native_events import NativeInteractionRequested

            delivery = ({"agui_interaction_delivery": (
                "native_interrupt" if isinstance(event, NativeInteractionRequested)
                else "legacy_approval"
            )} if isinstance(event, InteractionRequested) else {})
            new_source = event.source.model_copy(
                update={"metadata": {**event.source.metadata, "protocol": "ag-ui", **delivery}}
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
                persisted = _agent_helpers.persisted_resume_fingerprint(
                    str(resolved[interrupt_id].source.metadata.get("resume_fingerprint") or "")
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
        if any(
            isinstance(event, (RunCanceled, RunCompleted, RunFailed))
            and event.run_id == interrupted.run_id
            and event.seq > interrupted.seq
            for event in events
        ):
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
        if not checkpoint_id:
            framework_ref = (handle.native_ref.get("framework_ref") or {}).get("langgraph") or {}
            checkpoint_id = str(framework_ref.get("checkpoint_id") or "")
        for request in requests:
            interrupt = self._interrupt_from_interaction(request)
            pending[interrupt.id] = _PendingInterrupt(
                interrupt, checkpoint_id, request.interaction_kind,
            )
        if not pending:
            return None, False
        if not self._shared.executor.is_attached(handle):
            handle = await self._shared.executor.attach(
                self._shared.launch_context,
                handle,
            )
        adapter = self._shared.executor.adapter_for_handle(handle)
        restore = getattr(adapter, "restore_interaction_context", None)
        if callable(restore):
            await restore(handle, requests, events)
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
