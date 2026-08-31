"""Studio Run persistence on top of the canonical RuntimeExecutor."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ksadk.agui.a2ui_projection import project_a2ui_operations
from ksadk.conversations.projector import (
    project_conversation_item,
    project_interaction_conversation_item,
)
from ksadk.events.canonical import (
    ContinuationCreated,
    ContinuationResumed,
    ErrorInfo,
    InteractionRequested,
    InteractionResolved,
    ItemCompleted,
    ItemFailed,
    ItemStarted,
    ItemUpdated,
    OutputRef,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunProgress,
    RunStarted,
    RuntimeEvent,
    SourceRef,
    UsageReported,
    dump_runtime_event,
    parse_runtime_event,
)
from ksadk.events.canonical_store import session_event_to_runtime_event
from ksadk.events.content import (
    ContentSnapshot,
    DataContent,
    TextContent,
    ToolCallContent,
    ToolResultContent,
)
from ksadk.events.store import RuntimeEventStore
from ksadk.runtime import (
    CONVERSATION_PREPROCESSING_METADATA_KEY,
    PauseResult,
    ResumePayload,
    ResumeTarget,
    RuntimeExecutor,
    RuntimeLaunchContext,
    StartRequest,
)
from ksadk.sessions.base import BaseSessionService
from ksadk.sessions.local_service import LocalSessionService
from ksadk.studio.contracts import RunEvent, RunRecord, RunStatus, Usage
from ksadk.studio.errors import StudioError
from ksadk.studio.event_store import RunEventStore
from ksadk.studio.workspace import Workspace

_CANCEL_TIMEOUT_SECONDS = 2.0


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StudioRunSpec:
    """Product metadata plus the core launch context for one Studio run."""

    launch_context: RuntimeLaunchContext
    build_id: str
    agent_id: str
    model: str | None = None
    request_config: Mapping[str, Any] = field(default_factory=dict)
    manifest_sha256: str = ""
    plugin_bundle_root: Path | None = None


class StudioRunService:
    """Persist Studio state without selecting or wrapping a RuntimeAdapter."""

    def __init__(
        self,
        workspace: Workspace,
        executor: RuntimeExecutor,
        *,
        event_store: RunEventStore | None = None,
        session_service: BaseSessionService | None = None,
        runtime_events: RuntimeEventStore | None = None,
        plugin_runtime: Any | None = None,
    ) -> None:
        self.workspace = workspace
        self.executor = executor
        self.event_store = event_store or RunEventStore(workspace)
        self.session_service = session_service or LocalSessionService(
            project_dir=str(workspace.root)
        )
        self.runtime_events = runtime_events or RuntimeEventStore(self.session_service)
        self.plugin_runtime = plugin_runtime
        self._active_handles: dict[str, Any] = {}
        self._cancel_flags: dict[str, bool] = {}
        self._control_queues: dict[
            str,
            asyncio.Queue[tuple[str, ResumePayload | None, asyncio.Future[None] | None]],
        ] = {}
        self._waiting_modes: dict[str, str] = {}
        # Interaction resolution appends to one run-level event file.  Serialise
        # every decision for that run, not only identical interaction ids, so
        # two concurrently visible cards cannot overwrite each other's receipt.
        self._interaction_locks: dict[str, asyncio.Lock] = {}

    async def recover_interrupted(self) -> None:
        """Settle local runs left active across a Studio restart.

        An in-process handle cannot be resumed safely.  Keep its durable event
        history and record an explicit interruption; lease-backed hosted
        recovery remains owned by the Kernel composition root.
        """
        active = {RunStatus.RUNNING, RunStatus.WAITING_INPUT}
        for record in self.event_store.list_runs():
            if record.status not in active:
                continue
            record.status = RunStatus.INTERRUPTED
            record.completed_at = datetime.now(timezone.utc)
            record.error = {
                "code": "LOCAL_STUDIO_RESTARTED",
                "message": "本地 Studio 重启，运行未在本地恢复。",
            }
            self.event_store.save(record)
            self.event_store.append(
                record.id,
                "run.interrupted",
                {"reason": "local_studio_restarted", "recoverable": False},
            )

    async def events(self, run_id: str, *, after: int = 0) -> list[RunEvent]:
        """Read the durable Studio event timeline for API/SSE replay."""
        return self.event_store.events(run_id, after=after)

    async def run(
        self,
        spec: StudioRunSpec,
        user_input: str,
        *,
        runtime_input: Any = None,
        session_id: str | None = None,
        on_event: Callable[[RunEvent], None] | None = None,
    ) -> RunRecord:
        run_id = f"run_{uuid4().hex}"
        session = session_id or f"ses_{uuid4().hex}"
        runtime_type = spec.launch_context.runtime_type.strip().lower()
        record = RunRecord(
            id=run_id,
            build_id=spec.build_id,
            agent_id=spec.agent_id,
            session_id=session,
            trace_id=uuid4().hex,
            manifest_sha256=spec.manifest_sha256,
            runtime_type=runtime_type,
            model=str(spec.model or ""),
            collaboration_mode=str(spec.request_config.get("collaboration_mode") or ""),
            goal_objective=str(spec.request_config.get("goal_objective") or ""),
            input=user_input,
        )
        await self.session_service.create_session(spec.agent_id, "local-user", session)
        self.event_store.create(record)
        prepared_turn = await self._capture_pcm_evidence(record, spec, user_input)
        created = self.event_store.append(
            record.id,
            "run.created",
            {
                "runId": record.id,
                "buildId": spec.build_id,
                "sessionId": session,
                "traceId": record.trace_id,
                "runtimeType": runtime_type,
                "manifestSha256": spec.manifest_sha256,
                "model": record.model,
                "collaborationMode": record.collaboration_mode,
                "goalObjective": record.goal_objective,
            },
        )
        if on_event is not None:
            on_event(created)

        kernel_runtime = self._kernel_runtime_for_spec(spec)
        if kernel_runtime is not None:
            return await self._kernel_run(
                spec,
                user_input,
                record=record,
                on_event=on_event,
                kernel_runtime=kernel_runtime,
            )
        if spec.plugin_bundle_root is not None:
            return await self._plugin_run(
                spec,
                user_input,
                record=record,
                on_event=on_event,
            )

        started = time.monotonic()
        record.status = RunStatus.RUNNING
        record.started_at = datetime.now(timezone.utc)
        self.event_store.save(record)

        async def persist(runtime_event: RuntimeEvent) -> RunEvent:
            persisted = await self.runtime_events.append_one(record.session_id, runtime_event)
            event_type, data = project_runtime_event(
                persisted,
                session_id=record.session_id,
                public_run_id=record.id,
            )
            if isinstance(persisted, InteractionRequested):
                data["revision"] = 1
                conversation_item = data.get("conversationItem")
                if isinstance(conversation_item, dict):
                    item_payload = conversation_item.get("payload")
                    if isinstance(item_payload, dict):
                        item_payload["revision"] = 1
            stored = self.event_store.append(record.id, event_type, data)
            if on_event is not None:
                on_event(stored)
            return stored

        handle = None
        completed_text_by_item: dict[tuple[str, str], str] = {}
        streamed_text_by_item: dict[tuple[str, str], str] = {}
        terminal_output_refs: tuple[OutputRef, ...] = ()
        runtime_duration_ms: int | None = None
        item_phases: dict[tuple[str, str], str | None] = {}
        control_queue: asyncio.Queue[
            tuple[str, ResumePayload | None, asyncio.Future[None] | None]
        ] = asyncio.Queue()
        self._control_queues[run_id] = control_queue
        try:
            tool_approval_mode = str(spec.request_config.get("tool_approval_mode") or "")
            conversation_request: dict[str, Any] = {
                "messages": self._conversation_messages(
                    spec.agent_id,
                    session,
                    user_input,
                )
            }
            if tool_approval_mode:
                conversation_request["request_metadata"] = {
                    "tool_approval_mode": tool_approval_mode,
                }
            if prepared_turn is not None:
                conversation_request["prepared_turn"] = asdict(prepared_turn)
            request = StartRequest(
                input=runtime_input if runtime_input is not None else user_input,
                user_id="local-user",
                session_id=session,
                agent_id=spec.agent_id,
                model=spec.model,
                config=dict(spec.request_config),
                metadata={
                    "invocation_id": run_id,
                    CONVERSATION_PREPROCESSING_METADATA_KEY: conversation_request,
                    **self._native_session_metadata(spec.agent_id, session, runtime_type),
                },
            )
            handle = await self.executor.start(spec.launch_context, request)
            record.runtime_handle = handle.model_dump(mode="json")
            self.event_store.save(record)
            self._active_handles[run_id] = handle
            while True:
                terminal_seen = False
                should_resume = False
                async for event in self.executor.stream(handle):
                    await persist(event)
                    if self._cancel_flags.get(run_id):
                        raise asyncio.CancelledError()
                    if isinstance(event, ItemStarted) and event.item_kind == "message":
                        item_phases[(event.scope_id, event.item_id)] = event.phase
                    elif (
                        isinstance(event, ItemUpdated)
                        and event.item_kind == "message"
                        and item_phases.get(
                            (event.scope_id, event.item_id), "final_answer"
                        )
                        == "final_answer"
                    ):
                        text = event.update.text if isinstance(event.update, TextContent) else ""
                        item_key = (event.scope_id, event.item_id)
                        if event.op == "replace":
                            streamed_text_by_item[item_key] = text
                        else:
                            streamed_text_by_item[item_key] = (
                                streamed_text_by_item.get(item_key, "") + text
                            )
                    elif (
                        isinstance(event, ItemCompleted)
                        and event.item_kind == "message"
                        and item_phases.get(
                            (event.scope_id, event.item_id), "final_answer"
                        )
                        == "final_answer"
                    ):
                        completed_text_by_item[(event.scope_id, event.item_id)] = "".join(
                            part.text
                            for part in event.snapshot.parts
                            if isinstance(part, TextContent)
                        )
                    elif isinstance(event, UsageReported):
                        record.usage = Usage(
                            input_tokens=event.input_tokens,
                            output_tokens=event.output_tokens,
                            total_tokens=event.total_tokens,
                            cached_input_tokens=event.cached_tokens,
                            reasoning_output_tokens=event.reasoning_tokens,
                            reported=True,
                            source=str(event.source.framework or runtime_type),
                        )
                    elif isinstance(event, InteractionRequested):
                        record.status = RunStatus.WAITING_INPUT
                        self._waiting_modes[run_id] = "live"
                        self.event_store.save(record)
                        if event.interaction_kind == "approval":
                            self._persist_approval_surface(record, event, on_event=on_event)
                    elif isinstance(event, InteractionResolved):
                        record.status = RunStatus.RUNNING
                        self._waiting_modes.pop(run_id, None)
                        self.event_store.save(record)
                    elif isinstance(event, RunFailed):
                        terminal_seen = True
                        record.status = RunStatus.FAILED
                        record.error = {
                            "code": "RUNTIME_RUN_FAILED",
                            "message": str(event.error.message or "Runtime 运行失败"),
                        }
                    elif isinstance(event, RunCanceled):
                        terminal_seen = True
                        record.status = RunStatus.CANCELLED
                        record.error = {
                            "code": "RUN_CANCELLED",
                            "message": str(event.reason or "运行已取消"),
                        }
                    elif isinstance(event, RunInterrupted):
                        if event.reason == "user_pause":
                            record.status = RunStatus.PAUSED
                            should_resume = True
                        elif record.status == RunStatus.WAITING_INPUT:
                            self._waiting_modes[run_id] = "resume"
                            should_resume = True
                        else:
                            terminal_seen = True
                            record.status = RunStatus.INTERRUPTED
                            record.error = {
                                "code": "RUN_INTERRUPTED",
                                "message": event.reason or "运行已中断",
                            }
                        self.event_store.save(record)
                    elif isinstance(event, RunCompleted):
                        terminal_seen = True
                        record.status = RunStatus.COMPLETED
                        terminal_output_refs = event.output_refs
                        raw_duration = event.source.metadata.get("duration_ms")
                        if raw_duration is None:
                            metrics = event.source.metadata.get("metrics")
                            if isinstance(metrics, dict):
                                raw_duration = metrics.get("duration_ms")
                        if raw_duration is not None:
                            runtime_duration_ms = max(0, int(raw_duration))
                if terminal_seen:
                    break
                if not should_resume:
                    record.status = RunStatus.COMPLETED
                    break

                command, resume_payload, submit_ack = await control_queue.get()
                if command == "cancel" or self._cancel_flags.get(run_id):
                    raise asyncio.CancelledError()
                if command != "resume":
                    continue
                native_thread_id = handle.native_ref.get("thread_id")
                target = (
                    ResumeTarget(kind="thread_id", id=str(native_thread_id))
                    if native_thread_id
                    else ResumeTarget(kind="invocation_id", id=handle.run_id)
                )
                try:
                    handle = await self.executor.resume(
                        handle,
                        target,
                        resume_payload,
                    )
                except Exception as exc:
                    if submit_ack is not None and not submit_ack.done():
                        submit_ack.set_exception(exc)
                    raise
                if submit_ack is not None and not submit_ack.done():
                    submit_ack.set_result(None)
                self._active_handles[run_id] = handle
                record.runtime_handle = handle.model_dump(mode="json")
                record.status = RunStatus.RUNNING
                record.error = None
                self._waiting_modes.pop(run_id, None)
                resumed = self.event_store.append(
                    run_id,
                    "run.resumed",
                    {"runId": run_id, "runtimeHandle": record.runtime_handle},
                )
                if on_event is not None:
                    on_event(resumed)
                self.event_store.save(record)
            text_by_item = {**streamed_text_by_item, **completed_text_by_item}
            if terminal_output_refs:
                terminal_parts = [
                    text_by_item.get((ref.scope_id, ref.item_id), "")
                    for ref in terminal_output_refs
                ]
                record.output = "\n\n".join(part for part in terminal_parts if part)
            else:
                completed_parts = [part for part in completed_text_by_item.values() if part]
                fallback_parts = [part for part in streamed_text_by_item.values() if part]
                record.output = "\n\n".join(completed_parts or fallback_parts)
        except asyncio.CancelledError:
            cancel_result = "task_cancelled"
            if handle is not None and self.executor.is_attached(handle):
                try:
                    result = await asyncio.wait_for(
                        asyncio.shield(self.executor.cancel(handle)),
                        timeout=_CANCEL_TIMEOUT_SECONDS,
                    )
                    cancel_result = result.value
                except TimeoutError:
                    cancel_result = "cancel_timed_out"
                except Exception:  # best effort; close still owns resource cleanup
                    cancel_result = "cancel_failed"
            record.status = RunStatus.CANCELLED
            record.error = {"code": "RUN_CANCELLED", "message": "运行已取消"}
            cancelled_run_id = handle.run_id if handle is not None else run_id
            cancelled = RunCanceled(
                schema_version=2,
                event_id=f"evt_cancel_{uuid4().hex}",
                seq=len(self.event_store.events(run_id)) + 1,
                timestamp=time.time(),
                run_id=cancelled_run_id,
                scope_id=cancelled_run_id,
                source=SourceRef(
                    framework="ksadk",
                    metadata={"cancel_result": cancel_result},
                ),
                status="canceled",
                reason="cancelled",
            )
            await persist(cancelled)
            raise
        except Exception as exc:  # noqa: BLE001
            record.status = RunStatus.FAILED
            record.error = {"code": "RUNTIME_RUN_FAILED", "message": str(exc)}
            failed_run_id = handle.run_id if handle is not None else run_id
            failure = RunFailed(
                schema_version=2,
                event_id=f"evt_fail_{uuid4().hex}",
                seq=len(self.event_store.events(run_id)) + 1,
                timestamp=time.time(),
                run_id=failed_run_id,
                scope_id=failed_run_id,
                source=SourceRef(framework="ksadk"),
                status="failed",
                error=ErrorInfo(
                    code="RUNTIME_RUN_FAILED",
                    message=str(exc),
                    source="ksadk",
                    scope_id=failed_run_id,
                ),
            )
            await persist(failure)
        finally:
            if handle is not None and self.executor.is_attached(handle):
                try:
                    await asyncio.shield(self.executor.close(handle))
                except asyncio.CancelledError:
                    pass
                except Exception:  # cleanup is best effort; run state is already durable
                    pass
            self._active_handles.pop(run_id, None)
            self._cancel_flags.pop(run_id, None)
            self._control_queues.pop(run_id, None)
            self._waiting_modes.pop(run_id, None)
            record.completed_at = datetime.now(timezone.utc)
            if runtime_duration_ms is not None:
                record.duration_ms = runtime_duration_ms
                record.duration_source = "runtime"
            else:
                record.duration_ms = int((time.monotonic() - started) * 1000)
                record.duration_source = "studio"
            self.event_store.save(record)
            await self._sync_trace(record)
        return record

    async def _sync_trace(self, record: RunRecord) -> None:
        self.event_store.trace_store.sync(record, await self.events(record.id))

    async def _plugin_run(
        self,
        spec: StudioRunSpec,
        user_input: str,
        *,
        record: RunRecord,
        on_event: Callable[[RunEvent], None] | None,
    ) -> RunRecord:
        """Execute a composed Build without translating it into ADK/LangGraph.

        PluginHost retains the activation under the Studio session id.  Harness
        providers write canonical RuntimeEvents directly; providers that only
        implement the minimum request/result protocol receive a canonical
        envelope here so Studio still has one durable conversation/event model.
        """

        started = time.monotonic()
        record.status = RunStatus.RUNNING
        record.started_at = datetime.now(timezone.utc)
        record.runtime_handle = {
            "provider": "pluginhost",
            "bundleDigest": str(spec.request_config.get("plugin_bundle_digest") or ""),
            "activationKey": record.session_id,
        }
        self.event_store.save(record)
        rows_before = await self.session_service.get_events(record.session_id)
        after_seq = max((int(row.seq_id or 0) for row in rows_before), default=0)

        async def publish(events: list[RuntimeEvent]) -> None:
            for runtime_event in events:
                event_type, data = project_runtime_event(
                    runtime_event,
                    session_id=record.session_id,
                )
                stored = self.event_store.append(record.id, event_type, data)
                if on_event is not None:
                    on_event(stored)

        try:
            if self.plugin_runtime is None:
                raise StudioError(
                    "PLUGIN_RUNTIME_UNAVAILABLE",
                    "Studio 尚未配置 PluginHost Runtime",
                    status_code=503,
                )
            request_metadata = {
                key: value
                for key, value in {
                    "tool_approval_mode": spec.request_config.get("tool_approval_mode"),
                    "collaboration_mode": spec.request_config.get("collaboration_mode"),
                    "goal_objective": spec.request_config.get("goal_objective"),
                    "reasoning_effort": spec.request_config.get("effort"),
                }.items()
                if value not in {None, ""}
            }
            result = await self.plugin_runtime.execute(
                spec,
                {
                    "user_id": "local-user",
                    "session_id": record.session_id,
                    "invocation_id": record.id,
                    "messages": [{"role": "user", "content": user_input}],
                    "model": spec.model,
                    "request_metadata": request_metadata,
                },
                session_id=record.session_id,
            )
            if result.session_id != record.session_id:
                raise StudioError(
                    "PLUGIN_SESSION_MISMATCH",
                    "AgentProvider 返回了不同的 Session",
                    status_code=502,
                    details={
                        "expected": record.session_id,
                        "actual": result.session_id,
                    },
                )
            canonical = await self._plugin_events_after(
                record.session_id,
                after_seq=after_seq,
                run_id=record.id,
            )
            if not canonical:
                canonical = await self._persist_plugin_result_events(
                    record,
                    result.output_text,
                    usage=result.usage,
                )
            await publish(canonical)
            record.output = result.output_text
            record.status = RunStatus.COMPLETED
            _apply_plugin_usage(record, result.usage)
        except asyncio.CancelledError:
            record.status = RunStatus.CANCELLED
            record.error = {"code": "RUN_CANCELLED", "message": "运行已取消"}
            cancelled = await self.runtime_events.append_one(
                record.session_id,
                RunCanceled(
                    **_plugin_event_envelope(record, "run.canceled"),
                    status="canceled",
                    reason="cancelled",
                ),
            )
            await publish([cancelled])
            raise
        except Exception as exc:  # noqa: BLE001 - plugin boundary is typed below
            record.status = RunStatus.FAILED
            code = str(getattr(exc, "code", "PLUGIN_RUNTIME_FAILED"))
            record.error = {"code": code, "message": str(exc)}
            canonical = await self._plugin_events_after(
                record.session_id,
                after_seq=after_seq,
                run_id=record.id,
            )
            if canonical:
                await publish(canonical)
            else:
                failed = await self.runtime_events.append_one(
                    record.session_id,
                    RunFailed(
                        **_plugin_event_envelope(record, "run.failed"),
                        status="failed",
                        error=ErrorInfo(
                            code=code,
                            message=str(exc),
                            source="pluginhost",
                            scope_id=record.id,
                        ),
                    ),
                )
                await publish([failed])
        finally:
            record.completed_at = datetime.now(timezone.utc)
            record.duration_ms = int((time.monotonic() - started) * 1000)
            record.duration_source = "studio"
            self.event_store.save(record)
            await self._sync_trace(record)
        return record

    async def _plugin_events_after(
        self,
        session_id: str,
        *,
        after_seq: int,
        run_id: str,
    ) -> list[RuntimeEvent]:
        rows = await self.session_service.get_events(
            session_id,
            after_seq_id=after_seq,
        )
        events: list[RuntimeEvent] = []
        for row in rows:
            event = session_event_to_runtime_event(row)
            if event is not None and event.run_id == run_id:
                events.append(event)
        return events

    async def _persist_plugin_result_events(
        self,
        record: RunRecord,
        output_text: str,
        *,
        usage: Mapping[str, Any],
    ) -> list[RuntimeEvent]:
        item_id = f"{record.id}:assistant"
        part_id = f"{record.id}:text"
        snapshot = ContentSnapshot(
            parts=(TextContent(part_id=part_id, text=output_text),)
        )
        events: list[RuntimeEvent] = [
            RunStarted(
                **_plugin_event_envelope(record, "run.started"),
                status="running",
            ),
            ItemStarted(
                **_plugin_event_envelope(record, "item.started"),
                item_id=item_id,
                item_kind="message",
                phase="final_answer",
            ),
            ItemUpdated(
                **_plugin_event_envelope(record, "item.updated"),
                item_id=item_id,
                item_kind="message",
                op="append",
                update=TextContent(part_id=part_id, text=output_text),
            ),
            ItemCompleted(
                **_plugin_event_envelope(record, "item.completed"),
                item_id=item_id,
                item_kind="message",
                snapshot=snapshot,
            ),
        ]
        normalized = _normalized_plugin_usage(usage)
        if normalized["reported"]:
            events.append(
                UsageReported(
                    **_plugin_event_envelope(record, "usage.reported"),
                    input_tokens=normalized["input_tokens"],
                    output_tokens=normalized["output_tokens"],
                    total_tokens=normalized["total_tokens"],
                    cached_tokens=normalized["cached_tokens"],
                    reasoning_tokens=normalized["reasoning_tokens"],
                )
            )
        events.append(
            RunCompleted(
                **_plugin_event_envelope(record, "run.completed"),
                status="completed",
                output_refs=(
                    OutputRef(
                        scope_id=record.id,
                        item_id=item_id,
                        part_id=part_id,
                    ),
                ),
            )
        )
        return [
            await self.runtime_events.append_one(record.session_id, event)
            for event in events
        ]

    async def _kernel_run(
        self,
        spec: StudioRunSpec,
        user_input: str,
        *,
        record: RunRecord,
        on_event: Callable[[RunEvent], None] | None,
        kernel_runtime: Any,
    ) -> RunRecord:
        """kernel 路径（灰度 opt-in）：Studio run -> AgentControlCommand -> receipt。

        mutation 只走 kernel.submit；Studio RunEvent shape 保留，cursor 源自
        同一 Session seq（SessionEventSubscription.after_seq）。
        """
        from ksadk.kernel import ingress as _kernel_ingress

        started = time.monotonic()
        record.status = RunStatus.RUNNING
        record.started_at = datetime.now(timezone.utc)
        self.event_store.save(record)
        try:
            trusted = _kernel_ingress.trusted_context(
                source_kind="studio",
                source_ref=record.id,
                # The Worker polls the concrete Kernel Runtime instance, not
                # whichever synthetic ``local-agent`` happened to be inferred
                # from a Studio request.  A Studio Build may use the Kernel
                # path only after ``_kernel_runtime_for_spec`` established that
                # it is this Runtime's exact Agent/Runtime binding.
                tenant_id=str(kernel_runtime.config.tenant_id),
                agent_instance_id=str(kernel_runtime.config.agent_instance_id),
                session_id=record.session_id,
                operations=("enqueue",),
            )
            idempotency_key = str(
                (spec.request_config or {}).get("idempotency_key") or record.id
            )
            command = _kernel_ingress.map_studio_request(
                session_id=record.session_id,
                idempotency_key=idempotency_key,
                content=user_input,
                run_id=record.id,
                trusted=trusted,
            )
            receipt = await _kernel_ingress.submit_command(
                command, permit=trusted.permit
            )
            if receipt.status not in ("accepted", "duplicate"):
                record.status = RunStatus.FAILED
                record.error = {
                    "code": "kernel_command_rejected",
                    "message": f"agent kernel rejected command: {receipt.status}",
                }
                self.event_store.save(record)
                return record
            output_text = ""
            async for _seq, projected in _kernel_ingress.subscribe_projected(
                record.session_id,
                trusted=trusted,
                after_seq=int(receipt.accepted_seq or 0),
                projector=_studio_envelope_projection,
            ):
                if projected is None:
                    continue
                event_type, data = projected
                if event_type == "message.delta":
                    output_text += str(data.get("delta") or "")
                elif event_type == "run.completed":
                    output_text = str(data.get("output_text") or output_text)
                stored = self.event_store.append(record.id, event_type, data)
                if on_event is not None:
                    on_event(stored)
            record.output = output_text
            record.status = RunStatus.COMPLETED
            record.completed_at = datetime.now(timezone.utc)
            record.duration_ms = int((time.monotonic() - started) * 1000)
            self.event_store.save(record)
            await self._sync_trace(record)
            return record

        except Exception as exc:  # noqa: BLE001
            logger.exception("Studio kernel ingress failed for run %s", record.id)
            record.status = RunStatus.FAILED
            record.error = {
                "code": "kernel_ingress_failed",
                "message": str(exc),
            }
            record.completed_at = datetime.now(timezone.utc)
            record.duration_ms = int((time.monotonic() - started) * 1000)
            self.event_store.save(record)
            await self._sync_trace(record)
            return record

    @staticmethod
    def _kernel_runtime_for_spec(spec: StudioRunSpec) -> Any | None:
        """Return the active Kernel Runtime only for its bound Studio Build.

        An in-process AgentKernel owns one concrete RuntimeAdapter factory and
        its immutable startup defaults.  Merely observing that a Kernel is
        enabled is therefore insufficient: sending an arbitrary Studio Build
        through it can silently execute the default Adapter instead.  When the
        binding is not exact, the existing direct Studio path remains the
        compatible local behaviour; it is safer than manufacturing a false
        AgentControl/Scheduler success.
        """

        from ksadk.kernel.bootstrap import get_agent_kernel_runtime
        from ksadk.kernel.ingress import kernel_route_active

        if not kernel_route_active():
            return None
        runtime = get_agent_kernel_runtime()
        if runtime is None:
            return None
        active_context = getattr(runtime.config, "launch_context", None)
        if active_context is None:
            return None
        if str(getattr(active_context, "runtime_type", "")).strip().lower() != (
            spec.launch_context.runtime_type.strip().lower()
        ):
            return None
        try:
            active_project = Path(active_context.project_dir).resolve()
            build_project = Path(spec.launch_context.project_dir).resolve()
        except (OSError, TypeError, ValueError):
            return None
        if active_project != build_project:
            return None
        defaults = getattr(runtime.config, "start_request_defaults", {}) or {}
        bound_agent_id = str(
            defaults.get("agent_id") or runtime.config.agent_instance_id
        ).strip()
        if bound_agent_id != spec.agent_id:
            return None
        declared_instance = str(
            (spec.launch_context.config or {}).get("agent_instance_id") or ""
        ).strip()
        if declared_instance and declared_instance != str(
            runtime.config.agent_instance_id
        ):
            return None
        return runtime

    async def cancel_run(self, run_id: str) -> dict[str, str]:
        """Request cancellation; the flag and executor perform the actual stop."""
        self._cancel_flags[run_id] = True
        queue = self._control_queues.get(run_id)
        if queue is not None:
            queue.put_nowait(("cancel", None, None))
        handle = self._active_handles.get(run_id)
        if handle is not None and self.executor.is_attached(handle):
            try:
                await asyncio.wait_for(
                    asyncio.shield(self.executor.cancel(handle)),
                    timeout=_CANCEL_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                # The cancellation flag wakes the Studio state machine; final
                # transport teardown remains close()'s responsibility.
                pass
            except Exception:
                pass
        return {"runId": run_id, "status": "cancelling"}

    async def pause_run(self, run_id: str) -> dict[str, str]:
        record = self.event_store.get(run_id)
        if record.status == RunStatus.WAITING_INPUT:
            raise StudioError(
                "RUN_WAITING_INPUT",
                "当前运行正在等待交互输入，请先处理卡片",
                status_code=409,
            )
        if record.status != RunStatus.RUNNING:
            raise StudioError("RUN_NOT_RUNNING", "只有运行中的 Run 可以暂停", status_code=409)
        handle = self._active_handles.get(run_id)
        if handle is None or not self.executor.is_attached(handle):
            raise StudioError("RUN_NOT_ATTACHED", "运行句柄已不在当前进程", status_code=409)
        result = await self.executor.pause(handle)
        if result is PauseResult.NOT_SUPPORTED:
            raise StudioError(
                "RUN_PAUSE_UNSUPPORTED",
                "当前 Runtime 不支持可恢复暂停",
                status_code=409,
            )
        if result is not PauseResult.PAUSED_ACTIVE_TURN:
            raise StudioError("RUN_PAUSE_FAILED", "暂停运行失败", status_code=409)
        return {"runId": run_id, "status": "pausing"}

    async def resume_run(self, run_id: str) -> dict[str, str]:
        record = self.event_store.get(run_id)
        if record.status != RunStatus.PAUSED:
            raise StudioError("RUN_NOT_PAUSED", "只有已暂停的 Run 可以继续", status_code=409)
        queue = self._control_queues.get(run_id)
        if queue is None:
            raise StudioError(
                "RUN_RESUME_UNAVAILABLE",
                "Studio 进程已重启，当前暂停点无法恢复",
                status_code=409,
            )
        queue.put_nowait(("resume", ResumePayload(kind="free_text", data="继续运行"), None))
        return {"runId": run_id, "status": "resuming"}

    async def submit_interaction(
        self,
        run_id: str,
        interaction_id: str,
        *,
        name: str,
        data: dict[str, Any] | None = None,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        lock = self._interaction_locks.setdefault(run_id, asyncio.Lock())
        payload_data = dict(data or {})
        digest = hashlib.sha256(
            json.dumps(
                {"name": name, "data": payload_data},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        async with lock:
            return await self._submit_interaction_locked(
                run_id,
                interaction_id,
                name=name,
                data=payload_data,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                request_digest=digest,
            )

    async def _submit_interaction_locked(
        self,
        run_id: str,
        interaction_id: str,
        *,
        name: str,
        data: dict[str, Any],
        expected_revision: int,
        idempotency_key: str,
        request_digest: str,
    ) -> dict[str, Any]:
        events = self.event_store.events(run_id)
        prior = next(
            (
                event
                for event in reversed(events)
                if event.type == "a2ui.action"
                and str(event.data.get("interactionId") or "") == interaction_id
                and _interaction_revision(event.data) > 0
            ),
            None,
        )
        if prior is not None:
            if str(prior.data.get("idempotencyKey") or "") != idempotency_key:
                raise StudioError(
                    "INTERACTION_ALREADY_RESOLVED",
                    "该交互已由其他请求处理",
                    status_code=409,
                )
            if str(prior.data.get("requestDigest") or "") != request_digest:
                raise StudioError(
                    "INTERACTION_IDEMPOTENCY_CONFLICT",
                    "同一幂等键不能提交不同内容",
                    status_code=409,
                )
            record = self.event_store.get(run_id)
            if record.status == RunStatus.WAITING_INPUT:
                record.status = RunStatus.RUNNING
                self.event_store.save(record)
                self._waiting_modes.pop(run_id, None)
            return dict(prior.data.get("receipt") or {})

        interaction = next(
            (
                event
                for event in reversed(events)
                if event.type == "a2ui.interaction"
                and str(event.data.get("interactionId") or event.data.get("interaction_id") or "")
                == interaction_id
            ),
            None,
        )
        if interaction is None:
            raise StudioError("INTERACTION_NOT_FOUND", "交互请求不存在", status_code=404)
        revision = _interaction_revision(interaction.data)
        if revision < 1:
            raise StudioError(
                "INTERACTION_READ_ONLY",
                "交互请求缺少权威 revision，只能查看不能操作",
                status_code=409,
            )
        if expected_revision != revision:
            raise StudioError(
                "INTERACTION_REVISION_MISMATCH",
                "交互版本已变化，请刷新后重试",
                status_code=409,
                details={"expectedRevision": expected_revision, "actualRevision": revision},
            )
        record = self.event_store.get(run_id)
        if record.status != RunStatus.WAITING_INPUT:
            raise StudioError(
                "INTERACTION_NOT_PENDING",
                "该 Run 当前没有等待中的交互",
                status_code=409,
            )
        kind = str(interaction.data.get("kind") or "form")
        payload_data = {"decision": name, **data}
        payload = ResumePayload(
            kind="approval_decision" if kind == "approval" else "hitl_answer",
            call_id=interaction_id,
            data=payload_data,
        )
        mode = self._waiting_modes.get(run_id)
        handle = self._active_handles.get(run_id)
        if mode == "live":
            if handle is None or not self.executor.is_attached(handle):
                raise StudioError("INTERACTION_EXPIRED", "运行时交互已失效", status_code=409)
            try:
                await self.executor.submit(handle, payload)
            except Exception as exc:  # noqa: BLE001 - provider errors are user-safe here
                raise StudioError(
                    "INTERACTION_SUBMIT_FAILED",
                    str(exc),
                    status_code=409,
                ) from exc
        elif mode == "resume":
            queue = self._control_queues.get(run_id)
            if queue is None:
                raise StudioError("INTERACTION_EXPIRED", "运行时交互已失效", status_code=409)
            submit_ack = asyncio.get_running_loop().create_future()
            queue.put_nowait(("resume", payload, submit_ack))
            try:
                await submit_ack
            except Exception as exc:  # noqa: BLE001 - provider errors are user-safe here
                raise StudioError(
                    "INTERACTION_SUBMIT_FAILED",
                    str(exc),
                    status_code=409,
                ) from exc
        else:
            raise StudioError("INTERACTION_EXPIRED", "运行时交互已失效", status_code=409)

        next_revision = revision + 1
        resolved_data = {
            "runId": run_id,
            "interactionId": interaction_id,
            "callId": interaction_id,
            "name": name,
            "data": data,
            "revision": next_revision,
            "expectedRevision": expected_revision,
            "idempotencyKey": idempotency_key,
        }
        action_data = {
            "runId": run_id,
            "surfaceId": str(
                interaction.data.get("surfaceId") or interaction.data.get("surface_id") or ""
            ),
            "interactionId": interaction_id,
            "actionId": f"action-{interaction_id}",
            "name": name,
            "data": data,
            "revision": next_revision,
            "expectedRevision": expected_revision,
            "idempotencyKey": idempotency_key,
            "requestDigest": request_digest,
        }
        _, _, receipt = self.event_store.append_interaction_resolution(
            run_id,
            resolved_type=("approval.resolved" if kind == "approval" else "interaction.resolved"),
            resolved_data=resolved_data,
            action_data=action_data,
        )
        record.status = RunStatus.RUNNING
        self.event_store.save(record)
        self._waiting_modes.pop(run_id, None)
        return receipt

    async def _capture_pcm_evidence(
        self,
        record: RunRecord,
        spec: StudioRunSpec,
        user_input: str,
    ) -> Any | None:
        """Prepare once and persist the exact prompt/context input sent to Runtime."""

        try:
            from ksadk.conversations.runtime_preparation import build_run_input
            from ksadk.sessions.in_memory import InMemorySessionService

            temporary_sessions = InMemorySessionService()
            await temporary_sessions.create_session(
                agent_id=spec.agent_id,
                user_id="local-user",
                session_id=record.session_id,
            )
            config = dict(spec.request_config or {})
            prepared = await build_run_input(
                agent_id=spec.agent_id,
                user_id="local-user",
                session_id=record.session_id,
                messages=self._conversation_messages(
                    spec.agent_id,
                    record.session_id,
                    user_input,
                ),
                model=spec.model,
                instructions=str(config.get("instructions") or ""),
                agent_system=str(config.get("agent_system") or ""),
                agent_task=str(config.get("agent_task") or ""),
                prompt_integration_mode=str(config.get("prompt_integration_mode") or ""),
                context_engine_rollout=str(config.get("context_engine_rollout") or "") or None,
                memory_recall_enabled=config.get("memory_recall_enabled"),
                memory_write_rollout=str(config.get("memory_write_rollout") or "") or None,
                memory_enabled=config.get("memory_enabled"),
                memory_write_mode=str(config.get("memory_write_mode") or "candidate"),
                flush_before_compaction=bool(config.get("flush_before_compaction", True)),
                provider_ref=str(config.get("provider_ref") or "local-default"),
                runtime_type=spec.launch_context.runtime_type,
                deployment_mode=spec.launch_context.deployment_mode,
                invocation_id=record.id,
                session_service_provider=lambda: temporary_sessions,
                agent_max_input_tokens=config.get("max_input_tokens"),
                agent_reserve_output_tokens=config.get("reserve_output_tokens"),
            )
            record.context_plan = prepared.context_plan
            compiled = prepared.compiled_prompt
            shadow = prepared.shadow_context_plan or {}
            record.prompt_evidence = {
                "contentHash": (compiled or {}).get("prompt_content_hash"),
                "stablePrefixHash": (compiled or {}).get("prompt_stable_prefix_hash"),
                "sectionHashes": (compiled or {}).get("prompt_section_hashes", {}),
                "tokensBySection": (compiled or {}).get("prompt_tokens_by_section", {}),
                "estimatedTokens": (compiled or {}).get("prompt_estimated_tokens"),
                "sectionCount": (compiled or {}).get("prompt_section_count"),
                "integrationMode": shadow.get("integration_mode")
                or config.get("prompt_integration_mode")
                or "native",
                "accountingAccuracy": shadow.get("accounting_accuracy") or "opaque",
                "promptOwner": shadow.get("prompt_owner") or "runtime",
                "runtimeType": shadow.get("runtime_type") or spec.launch_context.runtime_type,
                "deploymentMode": shadow.get("deployment_mode")
                or spec.launch_context.deployment_mode,
                "capabilityHash": shadow.get("capability_hash"),
                "tokensByKind": shadow.get("tokens_by_kind", {}),
                "plannedInputTokens": shadow.get("planned_input_tokens"),
            }
            record.working_state = prepared.working_state
            for event in getattr(prepared, "memory_recall_events", []):
                self.event_store.append(
                    record.id,
                    event.get("type", "memory.recall.event"),
                    event,
                )
            self.event_store.save(record)
            return prepared
        except Exception:  # noqa: BLE001 - evidence collection is best effort
            logger.exception("Studio PCM evidence capture failed for run %s", record.id)
            return None

    def _persist_approval_surface(
        self,
        record: RunRecord,
        event: InteractionRequested,
        *,
        on_event: Callable[[RunEvent], None] | None,
    ) -> None:
        approval_id = event.interaction_id
        if not approval_id:
            return
        surface_id = f"approval-{approval_id}"
        detail_value = event.request.detail
        detail = detail_value if isinstance(detail_value, dict) else {}
        command = str(detail.get("command") or detail.get("reason") or "")
        kind = str(event.request.kind or "tool")
        components = [
            {
                "id": "root",
                "component": "Card",
                "title": "需要你的确认",
                "children": ["approval"],
            },
            {
                "id": "approval",
                "component": "ApprovalBar",
                "tool_name": kind,
                "summary": command or f"Agent 请求执行 {kind} 操作",
                "approve_label": "批准",
                "deny_label": "拒绝",
            },
        ]
        begin = self.event_store.append(
            record.id,
            "a2ui.surface.begin",
            {
                "runId": record.id,
                "surfaceId": surface_id,
                "a2uiOperations": [
                    {
                        "version": "v0.9",
                        "createSurface": {
                            "surfaceId": surface_id,
                            "catalogId": "https://a2ui.org/specification/v0_9/basic_catalog.json",
                        },
                    },
                    {
                        "version": "v0.9",
                        "updateComponents": {
                            "surfaceId": surface_id,
                            "components": components,
                        },
                    },
                ],
            },
        )
        interaction = self.event_store.append(
            record.id,
            "a2ui.interaction",
            {
                "runId": record.id,
                "surfaceId": surface_id,
                "interactionId": approval_id,
                "kind": "approval",
                "revision": 1,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "decision": {
                            "type": "string",
                            "enum": ["approve", "approve_session", "deny"],
                        }
                    },
                    "required": ["decision"],
                },
            },
        )
        if on_event is not None:
            on_event(begin)
            on_event(interaction)

    def _conversation_messages(
        self,
        agent_id: str,
        session_id: str,
        user_input: str,
    ) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for previous in self.event_store.list_runs(session_id=session_id):
            if previous.agent_id != agent_id or previous.status != RunStatus.COMPLETED:
                continue
            messages.append({"role": "user", "content": previous.input})
            messages.append({"role": "assistant", "content": previous.output})
        messages.append({"role": "user", "content": user_input})
        return messages

    def _native_session_metadata(
        self,
        agent_id: str,
        session_id: str,
        runtime_type: str,
    ) -> dict[str, str]:
        if runtime_type != "codex":
            return {}
        for previous in self.event_store.list_runs(session_id=session_id):
            if previous.agent_id != agent_id or previous.status != RunStatus.COMPLETED:
                continue
            native_ref = previous.runtime_handle.get("native_ref")
            native_ref = native_ref if isinstance(native_ref, dict) else {}
            thread_id = str(native_ref.get("thread_id") or "")
            if thread_id:
                return {"thread_id": thread_id}
        return {}


def _plugin_event_envelope(record: RunRecord, event_type: str) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "event_id": f"{record.id}:{event_type}",
        "seq": 0,
        "timestamp": time.time(),
        "run_id": record.id,
        "scope_id": record.id,
        "source": SourceRef(
            framework="ksadk",
            metadata={
                "runtime": "pluginhost",
                "agent_id": record.agent_id,
                "session_id": record.session_id,
                "build_id": record.build_id,
            },
        ),
    }


def _normalized_plugin_usage(usage: Mapping[str, Any]) -> dict[str, Any]:
    def number(*keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if value is not None:
                try:
                    return max(0, int(value))
                except (TypeError, ValueError):
                    return 0
        return 0

    normalized = {
        "input_tokens": number("input_tokens", "inputTokens", "prompt_tokens"),
        "output_tokens": number(
            "output_tokens", "outputTokens", "completion_tokens"
        ),
        "total_tokens": number("total_tokens", "totalTokens"),
        "cached_tokens": number("cached_tokens", "cachedTokens"),
        "reasoning_tokens": number("reasoning_tokens", "reasoningTokens"),
    }
    if not normalized["total_tokens"]:
        normalized["total_tokens"] = (
            normalized["input_tokens"] + normalized["output_tokens"]
        )
    normalized["reported"] = bool(usage) and any(
        key in usage
        for key in (
            "input_tokens",
            "inputTokens",
            "prompt_tokens",
            "output_tokens",
            "outputTokens",
            "completion_tokens",
            "total_tokens",
            "totalTokens",
        )
    )
    return normalized


def _interaction_revision(data: Mapping[str, Any]) -> int:
    """Treat malformed or historical interaction revisions as read-only."""

    try:
        return max(0, int(data.get("revision") or 0))
    except (TypeError, ValueError):
        return 0


def _apply_plugin_usage(record: RunRecord, usage: Mapping[str, Any]) -> None:
    normalized = _normalized_plugin_usage(usage)
    if not normalized["reported"]:
        return
    record.usage = Usage(
        input_tokens=normalized["input_tokens"],
        output_tokens=normalized["output_tokens"],
        total_tokens=normalized["total_tokens"],
        cached_input_tokens=normalized["cached_tokens"],
        reasoning_output_tokens=normalized["reasoning_tokens"],
        reported=True,
        source="pluginhost",
    )


def project_runtime_event(
    event: RuntimeEvent,
    *,
    session_id: str | None = None,
    public_run_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Project the canonical RuntimeEvent into Studio's persisted event view.

    公开承诺字段（契约声明见 ``ksadk/events/projections.py``，执行形态为
    ``tests/protocol/test_cross_projection_golden.py``）：
    - 所有事件 payload 必含 ``runId``/``scopeId``；
    - ``message.*``/``thinking.*`` 含 ``itemId``（delta 另含 ``partId``）；
    - ``tool.*``/``command.*``/``approval.*``/``a2ui.*`` 含 ``itemId``；
    - ``a2ui.surface.*`` 含 ``surfaceId`` 与 ``a2uiOperations`` 列表。

    内部不保证字段：除上述外的 payload 附加键、事件类型枚举的完备性
    （新增 canonical 事件类型在未适配前可能整条丢弃）。
    """

    if isinstance(event, RunStarted):
        projected = "run.started"
        payload: dict[str, Any] = {}
    elif isinstance(event, RunProgress):
        projected = "run.progress"
        payload = {"progress": event.progress, "message": event.message}
    elif isinstance(event, RunCompleted):
        projected = "run.completed"
        payload = {}
    elif isinstance(event, RunFailed):
        projected = "run.failed"
        payload = {"error": event.error.message or ""}
    elif isinstance(event, RunCanceled):
        projected = "run.cancelled"
        payload = {"reason": event.reason or ""}
    elif isinstance(event, RunInterrupted):
        projected = "run.paused" if event.reason == "user_pause" else "run.interrupted"
        payload = {"reason": event.reason or ""}
    elif isinstance(event, ItemUpdated) and event.item_kind in {"message", "reasoning"}:
        text = event.update.text if isinstance(event.update, TextContent) else ""
        is_thinking = event.item_kind == "reasoning"
        projected = "thinking.delta" if is_thinking else "message.delta"
        payload = {"text": text}
    elif isinstance(event, ItemCompleted) and event.item_kind in {"message", "reasoning"}:
        text = ""
        for part in event.snapshot.parts:
            if isinstance(part, TextContent):
                text = part.text
                break
        is_thinking = event.item_kind == "reasoning"
        projected = "thinking.completed" if is_thinking else "message.completed"
        payload = {"text": text}
    elif isinstance(event, ItemStarted) and event.item_kind == "tool_call":
        tool_part = _first_content(event.initial, ToolCallContent) if event.initial else None
        if tool_part is not None and tool_part.name == "codex.command":
            projected = "command.started"
            args = tool_part.arguments if isinstance(tool_part.arguments, dict) else {}
            payload = {
                "callId": tool_part.call_id,
                "command": str(args.get("command") or ""),
                "cwd": str(args.get("cwd") or ""),
                "commandActions": args.get("command_actions") or [],
            }
        elif tool_part is not None:
            projected = "tool.started"
            payload = {
                "callId": tool_part.call_id,
                "tool": tool_part.name,
                "args": tool_part.arguments,
            }
        else:
            projected = "tool.started"
            payload = {}
    elif isinstance(event, ItemCompleted) and event.item_kind == "tool_call":
        tool_call = _first_content(event.snapshot, ToolCallContent)
        tool_result = _first_content(event.snapshot, ToolResultContent)
        if tool_call is not None and tool_call.name == "codex.command":
            projected = "command.completed"
            result = (
                tool_result.result
                if tool_result is not None and isinstance(tool_result.result, dict)
                else {}
            )
            payload = {
                "callId": tool_call.call_id,
                "status": str(result.get("status") or ""),
                "exitCode": result.get("exit_code"),
                "durationMs": result.get("duration_ms"),
                "output": str(result.get("output") or ""),
            }
        else:
            projected = "tool.completed"
            result = (
                tool_result.result
                if tool_result is not None and isinstance(tool_result.result, dict)
                else {}
            )
            tool_status = str(result.get("status") or "")
            if tool_result is not None and tool_result.is_error:
                tool_status = "failed"
            call_id = (tool_call or tool_result).call_id if (tool_call or tool_result) else ""
            payload = {
                "callId": call_id,
                "tool": tool_call.name if tool_call is not None else "",
                "status": tool_status,
                "durationMs": result.get("duration_ms"),
                "output": str(result.get("output") or ""),
            }
            if tool_result is not None and tool_result.is_error:
                payload["error"] = str(result.get("error") or "")
    elif (
        isinstance(event, (ItemStarted, ItemUpdated, ItemCompleted))
        and event.item_kind == "data"
        and event.source.protocol == "a2ui"
    ):
        projected, payload = _project_a2ui_surface(event)
    elif isinstance(event, InteractionRequested):
        if event.interaction_kind == "approval":
            projected = "approval.requested"
            payload = {
                "approvalId": event.interaction_id,
                "callId": event.request.call_id or "",
                "kind": event.request.kind,
                "detail": event.request.detail,
            }
        else:
            projected = "a2ui.interaction"
            payload = {
                "interactionId": event.interaction_id,
                "kind": "form",
                "inputSchema": {},
            }
    elif isinstance(event, InteractionResolved):
        if event.interaction_kind == "approval":
            projected = "approval.resolved"
            payload = {
                "approvalId": event.interaction_id,
                "callId": "",
                "decision": "",
            }
        else:
            projected = "a2ui.action"
            payload = {"interactionId": event.interaction_id}
    elif isinstance(event, ContinuationCreated):
        projected = "checkpoint.created"
        payload = {
            "checkpointId": event.continuation_id,
            "granularity": event.ref.get("granularity", "snapshot"),
            "resumable": event.resumable,
        }
    elif isinstance(event, ContinuationResumed):
        projected = "checkpoint.resumed"
        payload = {
            "checkpointId": event.continuation_id,
            "resumeAttemptId": event.resume_attempt_id,
        }
    elif isinstance(event, UsageReported):
        projected = "usage.reported"
        payload = {
            "inputTokens": event.input_tokens,
            "outputTokens": event.output_tokens,
            "totalTokens": event.total_tokens,
            "cachedTokens": event.cached_tokens,
            "reasoningTokens": event.reasoning_tokens,
        }
    else:
        projected = event.event_type
        payload = {}

    _attach_studio_identity(payload, event)
    if public_run_id and public_run_id != event.run_id:
        payload["runtimeRunId"] = event.run_id
        payload["runId"] = public_run_id
    payload["runtimeEvent"] = dump_runtime_event(event)
    # Additive only: legacy Studio/Web consumers keep reading the established
    # event type and payload keys.  New surfaces may opt into this typed,
    # identity-aware representation without reconstructing one from text.
    payload["conversationItem"] = project_conversation_item(
        event,
        session_id=session_id,
        run_id=public_run_id,
    ).model_dump(by_alias=True, exclude_none=True, mode="json")
    return projected, payload


def _first_content(
    snapshot: ContentSnapshot | None, content_type: type
) -> Any | None:
    if snapshot is None:
        return None
    for part in snapshot.parts:
        if isinstance(part, content_type):
            return part
    return None


def _project_a2ui_surface(
    event: ItemStarted | ItemUpdated | ItemCompleted,
) -> tuple[str, dict[str, Any]]:
    if isinstance(event, ItemStarted):
        projected = "a2ui.surface.begin"
        data_parts = event.initial.parts if event.initial is not None else ()
    elif isinstance(event, ItemUpdated):
        projected = "a2ui.surface.update"
        data_parts = (event.update,) if isinstance(event.update, DataContent) else ()
    else:
        projected = "a2ui.surface.end"
        data_parts = event.snapshot.parts

    surface_id = str(event.source.metadata.get("surface_id") or "")
    operations: list[dict[str, Any]] = []
    for part in data_parts:
        if isinstance(part, DataContent):
            data = part.data
            if isinstance(data, list):
                operations.extend(dict(op) for op in data if isinstance(op, Mapping))
    if not operations:
        operations = project_a2ui_operations(projected, {"surface_id": surface_id})
    payload: dict[str, Any] = {
        "surfaceId": surface_id,
        "a2uiOperations": operations,
    }
    return projected, payload


def _attach_studio_identity(payload: dict[str, Any], event: RuntimeEvent) -> None:
    """Attach §8.4 identity fields (runId/scopeId/itemId/partId/operation)."""
    payload["runId"] = event.run_id
    payload["scopeId"] = event.scope_id
    if isinstance(event, (ItemStarted, ItemUpdated, ItemCompleted, ItemFailed)):
        payload["itemId"] = event.item_id
    if isinstance(event, ItemUpdated):
        payload["operation"] = event.op
    if isinstance(event, ItemUpdated) and hasattr(event.update, "part_id"):
        payload["partId"] = event.update.part_id
    if isinstance(event, ItemStarted) and event.initial is not None:
        for part in event.initial.parts:
            if hasattr(part, "part_id"):
                payload["partId"] = part.part_id
                break
    if isinstance(event, ItemCompleted):
        for part in event.snapshot.parts:
            if hasattr(part, "part_id"):
                payload["partId"] = part.part_id
                break
    if isinstance(event, (InteractionRequested, InteractionResolved)):
        payload["itemId"] = event.interaction_id
    if isinstance(event, (ContinuationCreated, ContinuationResumed)):
        payload["itemId"] = event.continuation_id


__all__ = ["StudioRunService", "StudioRunSpec", "project_runtime_event"]


def _studio_envelope_projection(envelope) -> tuple[str, dict[str, Any]] | None:
    """Session envelope -> Studio RunEvent 投影；cursor 仍用 envelope.seq。"""

    payload = envelope.payload or {}
    # Kernel RuntimeEvent is persisted as a typed runtime/v2 SessionEvent
    # envelope.  Reuse the exact same projector as the direct Studio path so
    # a consumer receives its additive ConversationItem regardless of which
    # ingress delivered the Run.  Legacy event families retain the historical
    # text-only fallback below.
    if envelope.family == "runtime" and envelope.family_version == 2:
        try:
            runtime_event = parse_runtime_event(payload)
        except (TypeError, ValueError):
            # A damaged/unknown runtime payload must not make old Studio
            # subscriptions fail.  Preserve its event type for legacy
            # observability while refusing to invent a typed conversation item.
            return envelope.event_type, {"runId": envelope.run_id or ""}
        return project_runtime_event(runtime_event, session_id=envelope.session_id)
    if envelope.family == "interaction" and envelope.family_version == 1:
        conversation_item = project_interaction_conversation_item(envelope)
        if conversation_item is None:
            # Keep a malformed durable fact observable without reusing an
            # actionable legacy event name.  Existing Studio reducers turn
            # ``approval.requested`` into a submit button, so emitting that
            # name without an authoritative revision would bypass the typed
            # ConversationItem fail-closed boundary.
            payload = envelope.payload or {}
            return envelope.event_type, {
                "runId": envelope.run_id or str(payload.get("run_id") or ""),
                "itemId": str(payload.get("interaction_id") or envelope.event_id),
                "interactionReadOnly": True,
            }
        projected = _project_interaction_envelope_legacy(envelope)
        if projected is None:
            return None
        event_type, data = projected
        data["conversationItem"] = conversation_item.model_dump(
            by_alias=True,
            exclude_none=True,
            mode="json",
        )
        return event_type, data
    if envelope.event_type == "run.completed":
        return "run.completed", {
            "runId": envelope.run_id or "",
            "output_text": str(payload.get("output_text") or ""),
        }
    text = str(payload.get("delta") or payload.get("text") or "")
    if text:
        return "message.delta", {"delta": text}
    return None


def _project_interaction_envelope_legacy(
    envelope,
) -> tuple[str, dict[str, Any]] | None:
    """Keep established Studio event names while Interaction/v1 owns truth."""

    payload = envelope.payload or {}
    interaction_id = str(payload.get("interaction_id") or "")
    interaction_kind = str(payload.get("kind") or "")
    revision = payload.get("revision")
    request = payload.get("request")
    request = request if isinstance(request, Mapping) else {}
    presentation = request.get("presentation")
    presentation = presentation if isinstance(presentation, Mapping) else {}
    common: dict[str, Any] = {
        "runId": envelope.run_id or str(payload.get("run_id") or ""),
        "itemId": interaction_id,
        "interactionId": interaction_id,
    }
    # Expose the value as received for legacy/read-only diagnostics.  Only the
    # typed ConversationItem validates it as an authoritative writable token.
    if revision is not None:
        common["revision"] = revision

    if envelope.event_type == "interaction.requested":
        if interaction_kind == "approval":
            return "approval.requested", {
                **common,
                "approvalId": interaction_id,
                "callId": "",
                "kind": str(presentation.get("title") or interaction_kind),
                "detail": presentation.get("description"),
            }
        return "a2ui.interaction", {
            **common,
            "kind": interaction_kind or "form",
            "inputSchema": (
                dict(request.get("request_schema"))
                if isinstance(request.get("request_schema"), Mapping)
                else {}
            ),
        }
    if envelope.event_type in {
        "interaction.resolved",
        "interaction.cancelled",
        "interaction.expired",
    }:
        outcome = str(
            payload.get("outcome")
            or (
                "cancelled"
                if envelope.event_type == "interaction.cancelled"
                else "expired"
                if envelope.event_type == "interaction.expired"
                else ""
            )
        )
        if interaction_kind == "approval":
            return "approval.resolved", {
                **common,
                "approvalId": interaction_id,
                "callId": "",
                "decision": outcome,
            }
        return "a2ui.action", {**common, "name": outcome}
    return None
