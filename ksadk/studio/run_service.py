"""Studio Run persistence on top of the canonical RuntimeExecutor."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ksadk.agui.a2ui_projection import project_a2ui_operations
from ksadk.events.runtime_event import EventType, RuntimeEvent
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


@dataclass(frozen=True)
class StudioRunSpec:
    """Product metadata plus the core launch context for one Studio run."""

    launch_context: RuntimeLaunchContext
    build_id: str
    agent_id: str
    model: str | None = None
    request_config: Mapping[str, Any] = field(default_factory=dict)
    manifest_sha256: str = ""


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
    ) -> None:
        self.workspace = workspace
        self.executor = executor
        self.event_store = event_store or RunEventStore(workspace)
        self.session_service = session_service or LocalSessionService(
            project_dir=str(workspace.root)
        )
        self.runtime_events = runtime_events or RuntimeEventStore(self.session_service)
        self._active_handles: dict[str, Any] = {}
        self._cancel_flags: dict[str, bool] = {}
        self._control_queues: dict[str, asyncio.Queue[tuple[str, ResumePayload | None]]] = {}
        self._waiting_modes: dict[str, str] = {}

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

        async def persist(runtime_event: RuntimeEvent) -> RunEvent:
            return await self._persist_event(record, runtime_event, on_event=on_event)

        await persist(
            RuntimeEvent.create(
                EventType.RUN_PROGRESS,
                agent_id=spec.agent_id,
                user_id="local-user",
                session_id=session,
                invocation_id=run_id,
                seq_id=0,
                trace_id=record.trace_id,
                payload={
                    "status": "created",
                    "native_event": "run.created",
                    "native_data": {
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
                },
            )
        )
        await persist(
            RuntimeEvent.create(
                EventType.USER_MESSAGE,
                agent_id=spec.agent_id,
                user_id="local-user",
                session_id=session,
                invocation_id=run_id,
                seq_id=0,
                turn_id=f"turn_{run_id}",
                trace_id=record.trace_id,
                payload={"message_id": f"user:{run_id}", "text": user_input},
            )
        )

        started = time.monotonic()
        record.status = RunStatus.RUNNING
        record.started_at = datetime.now(timezone.utc)
        self.event_store.save(record)

        handle = None
        final_text = ""
        streamed_final = ""
        runtime_duration_ms: int | None = None
        control_queue: asyncio.Queue[tuple[str, ResumePayload | None]] = asyncio.Queue()
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
            request = StartRequest(
                input=runtime_input if runtime_input is not None else user_input,
                user_id="local-user",
                session_id=session,
                agent_id=spec.agent_id,
                model=spec.model,
                config=dict(spec.request_config),
                metadata={
                    "invocation_id": run_id,
                    "trace_id": record.trace_id,
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
                    if event.event_type == EventType.TEXT_DELTA and event.phase == "final_answer":
                        text = str(event.payload.get("text") or "")
                        if event.payload.get("replace"):
                            streamed_final = text
                        else:
                            streamed_final += text
                    elif (
                        event.event_type == EventType.TEXT_COMPLETED
                        and event.phase == "final_answer"
                    ):
                        final_text = str(event.payload.get("text") or "")
                    elif event.event_type == EventType.USAGE_REPORTED:
                        record.usage = Usage(
                            input_tokens=int(event.payload.get("input_tokens") or 0),
                            output_tokens=int(event.payload.get("output_tokens") or 0),
                            total_tokens=int(event.payload.get("total_tokens") or 0),
                            cached_input_tokens=int(event.payload.get("cached_tokens") or 0),
                            reasoning_output_tokens=int(event.payload.get("reasoning_tokens") or 0),
                            reported=True,
                            source=str(event.payload.get("source") or runtime_type),
                        )
                    elif event.event_type in {
                        EventType.APPROVAL_REQUESTED,
                        EventType.A2UI_INTERACTION,
                    }:
                        record.status = RunStatus.WAITING_INPUT
                        self._waiting_modes[run_id] = "live"
                        self.event_store.save(record)
                        if event.event_type == EventType.APPROVAL_REQUESTED:
                            await self._persist_approval_surface(
                                record,
                                event,
                                on_event=on_event,
                            )
                    elif event.event_type in {
                        EventType.APPROVAL_RESOLVED,
                        EventType.A2UI_ACTION,
                    }:
                        record.status = RunStatus.RUNNING
                        self._waiting_modes.pop(run_id, None)
                        self.event_store.save(record)
                    elif event.event_type == EventType.RUN_FAILED:
                        terminal_seen = True
                        record.status = RunStatus.FAILED
                        record.error = {
                            "code": "RUNTIME_RUN_FAILED",
                            "message": str(event.payload.get("error") or "Runtime 运行失败"),
                        }
                    elif event.event_type == EventType.RUN_CANCELED:
                        terminal_seen = True
                        record.status = RunStatus.CANCELLED
                        record.error = {
                            "code": "RUN_CANCELLED",
                            "message": str(event.payload.get("status") or "运行已取消"),
                        }
                    elif event.event_type == EventType.RUN_INTERRUPTED:
                        interrupted_status = str(event.payload.get("status") or "")
                        if interrupted_status == "paused":
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
                                "message": interrupted_status or "运行已中断",
                            }
                        self.event_store.save(record)
                    elif event.event_type == EventType.RUN_COMPLETED:
                        terminal_seen = True
                        record.status = RunStatus.COMPLETED
                        raw_duration = event.payload.get("duration_ms")
                        if raw_duration is not None:
                            runtime_duration_ms = max(0, int(raw_duration))
                if terminal_seen:
                    break
                if not should_resume:
                    record.status = RunStatus.COMPLETED
                    break

                command, resume_payload = await control_queue.get()
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
                handle = await self.executor.resume(
                    handle,
                    target,
                    resume_payload,
                )
                self._active_handles[run_id] = handle
                record.runtime_handle = handle.model_dump(mode="json")
                record.status = RunStatus.RUNNING
                record.error = None
                self._waiting_modes.pop(run_id, None)
                await persist(
                    RuntimeEvent.create(
                        EventType.RUN_PROGRESS,
                        agent_id=spec.agent_id,
                        user_id="local-user",
                        session_id=session,
                        invocation_id=run_id,
                        seq_id=0,
                        trace_id=record.trace_id,
                        payload={
                            "status": "running",
                            "native_event": "run.resumed",
                            "native_data": {
                                "runId": run_id,
                                "runtimeHandle": record.runtime_handle,
                            },
                        },
                    )
                )
                self.event_store.save(record)
            record.output = final_text or streamed_final
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
            cancelled = RuntimeEvent.create(
                EventType.RUN_CANCELED,
                agent_id=spec.agent_id,
                user_id="local-user",
                session_id=session,
                invocation_id=run_id,
                seq_id=0,
                trace_id=record.trace_id,
                payload={"status": "cancelled", "cancel_result": cancel_result},
            )
            await persist(cancelled)
            raise
        except Exception as exc:  # noqa: BLE001
            record.status = RunStatus.FAILED
            record.error = {"code": "RUNTIME_RUN_FAILED", "message": str(exc)}
            failure = RuntimeEvent.create(
                EventType.RUN_FAILED,
                agent_id=spec.agent_id,
                user_id="local-user",
                session_id=session,
                invocation_id=run_id,
                seq_id=0,
                trace_id=record.trace_id,
                payload={"status": "failed", "error": str(exc)},
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

    async def events(self, run_id: str, *, after: int = 0) -> list[RunEvent]:
        record = self.event_store.get(run_id)
        events = await self.runtime_events.list(
            record.session_id,
            after_seq_id=after,
            invocation_id=run_id,
        )
        return [self._project_event(run_id, event) for event in events]

    async def recover_interrupted(self) -> int:
        recovered = 0
        active_statuses = {
            RunStatus.CREATED,
            RunStatus.RUNNING,
            RunStatus.PAUSED,
            RunStatus.WAITING_INPUT,
        }
        for record in self.event_store.list_runs():
            if record.status not in active_statuses:
                continue
            await self.session_service.create_session(
                record.agent_id,
                "local-user",
                record.session_id,
            )
            events = await self.runtime_events.list(
                record.session_id,
                invocation_id=record.id,
            )
            terminal = next(
                (
                    event
                    for event in reversed(events)
                    if event.event_type
                    in {
                        EventType.RUN_COMPLETED,
                        EventType.RUN_FAILED,
                        EventType.RUN_CANCELED,
                        EventType.RUN_INTERRUPTED,
                    }
                    and not (
                        event.event_type == EventType.RUN_INTERRUPTED
                        and event.payload.get("status") == "paused"
                    )
                ),
                None,
            )
            if terminal is None:
                terminal_event = RuntimeEvent.create(
                    EventType.RUN_INTERRUPTED,
                    agent_id=record.agent_id,
                    user_id="local-user",
                    session_id=record.session_id,
                    invocation_id=record.id,
                    seq_id=0,
                    trace_id=record.trace_id,
                    payload={
                        "status": "interrupted",
                        "reason": "studio_restarted",
                    },
                )
                await self._persist_event(record, terminal_event)
            else:
                terminal_event = terminal

            if terminal_event.event_type == EventType.RUN_COMPLETED:
                record.status = RunStatus.COMPLETED
                record.error = None
            elif terminal_event.event_type == EventType.RUN_FAILED:
                record.status = RunStatus.FAILED
                record.error = {
                    "code": "RUNTIME_RUN_FAILED",
                    "message": str(terminal_event.payload.get("error") or "Agent 运行失败"),
                }
            elif terminal_event.event_type == EventType.RUN_CANCELED:
                record.status = RunStatus.CANCELLED
                record.error = {"code": "RUN_CANCELLED", "message": "运行已取消"}
            else:
                record.status = RunStatus.INTERRUPTED
                record.error = {
                    "code": "RUN_INTERRUPTED",
                    "message": "Studio 重启后无法重新 attach 上一次本地运行",
                }
            record.completed_at = datetime.fromtimestamp(
                terminal_event.timestamp,
                tz=timezone.utc,
            )
            if record.started_at is not None:
                record.duration_ms = max(
                    0,
                    int((record.completed_at - record.started_at).total_seconds() * 1000),
                )
            self.event_store.save(record)
            await self._sync_trace(record)
            recovered += 1
        return recovered

    async def _persist_event(
        self,
        record: RunRecord,
        event: RuntimeEvent,
        *,
        on_event: Callable[[RunEvent], None] | None = None,
    ) -> RunEvent:
        if event.session_id != record.session_id:
            raise ValueError("RuntimeEvent session_id does not match Studio RunRecord")
        if event.invocation_id != record.id:
            raise ValueError("RuntimeEvent invocation_id does not match Studio RunRecord")
        if event.trace_id is None:
            event = event.model_copy(update={"trace_id": record.trace_id})
        elif event.trace_id != record.trace_id:
            raise ValueError("RuntimeEvent trace_id does not match Studio RunRecord")
        stored = await self.runtime_events.append_one(event)
        projected = self._project_event(record.id, stored)
        if on_event is not None:
            on_event(projected)
        return projected

    @staticmethod
    def _project_event(run_id: str, event: RuntimeEvent) -> RunEvent:
        event_type, data = project_runtime_event(event)
        return RunEvent(
            id=event.seq_id,
            run_id=run_id,
            type=event_type,
            data=data,
            created_at=datetime.fromtimestamp(event.timestamp, tz=timezone.utc),
        )

    async def _sync_trace(self, record: RunRecord) -> None:
        events = await self.events(record.id)
        self.event_store.trace_store.sync(record, events)

    async def cancel_run(self, run_id: str) -> dict[str, str]:
        """Request cancellation; the flag and executor perform the actual stop."""
        self._cancel_flags[run_id] = True
        queue = self._control_queues.get(run_id)
        if queue is not None:
            queue.put_nowait(("cancel", None))
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
        queue.put_nowait(("resume", ResumePayload(kind="free_text", data="继续运行")))
        return {"runId": run_id, "status": "resuming"}

    async def submit_interaction(
        self,
        run_id: str,
        interaction_id: str,
        *,
        name: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = self.event_store.get(run_id)
        if record.status != RunStatus.WAITING_INPUT:
            raise StudioError(
                "INTERACTION_NOT_PENDING",
                "该 Run 当前没有等待中的交互",
                status_code=409,
            )
        events = await self.events(run_id)
        prior = [
            event
            for event in events
            if event.type == "a2ui.action"
            and str(event.data.get("interactionId") or event.data.get("interaction_id") or "")
            == interaction_id
        ]
        if prior:
            return {"runId": run_id, "interactionId": interaction_id, "status": "resolved"}

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
        kind = str(interaction.data.get("kind") or "form")
        payload_data = {"decision": name, **dict(data or {})}
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
            except (RuntimeError, ValueError) as exc:
                raise StudioError(
                    "INTERACTION_SUBMIT_FAILED",
                    str(exc),
                    status_code=409,
                ) from exc
        elif mode == "resume":
            queue = self._control_queues.get(run_id)
            if queue is None:
                raise StudioError("INTERACTION_EXPIRED", "运行时交互已失效", status_code=409)
            queue.put_nowait(("resume", payload))
        else:
            raise StudioError("INTERACTION_EXPIRED", "运行时交互已失效", status_code=409)

        resolved_type = (
            EventType.APPROVAL_RESOLVED if kind == "approval" else EventType.RUN_PROGRESS
        )
        resolved_payload = (
            {
                "approval_id": interaction_id,
                "call_id": interaction_id,
                "decision": name,
                "data": dict(data or {}),
            }
            if kind == "approval"
            else {
                "status": "running",
                "native_event": "interaction.resolved",
                "native_data": {
                    "runId": run_id,
                    "interactionId": interaction_id,
                    "callId": interaction_id,
                    "name": name,
                    "data": dict(data or {}),
                },
            }
        )
        resolved = await self._persist_event(
            record,
            RuntimeEvent.create(
                resolved_type,
                agent_id=record.agent_id,
                user_id="local-user",
                session_id=record.session_id,
                invocation_id=record.id,
                seq_id=0,
                trace_id=record.trace_id,
                payload=resolved_payload,
            ),
        )
        surface_id = str(
            interaction.data.get("surfaceId") or interaction.data.get("surface_id") or ""
        )
        action = await self._persist_event(
            record,
            RuntimeEvent.create(
                EventType.A2UI_ACTION,
                agent_id=record.agent_id,
                user_id="local-user",
                session_id=record.session_id,
                invocation_id=record.id,
                seq_id=0,
                trace_id=record.trace_id,
                payload={
                    "run_id": run_id,
                    "surface_id": surface_id,
                    "interaction_id": interaction_id,
                    "action_id": f"action-{interaction_id}",
                    "name": name,
                    "data": dict(data or {}),
                },
            ),
        )
        record.status = RunStatus.RUNNING
        self.event_store.save(record)
        self._waiting_modes.pop(run_id, None)
        return {
            "runId": run_id,
            "interactionId": interaction_id,
            "status": "resolved",
            "eventId": action.id,
            "resolutionEventId": resolved.id,
        }

    async def _persist_approval_surface(
        self,
        record: RunRecord,
        event: RuntimeEvent,
        *,
        on_event: Callable[[RunEvent], None] | None,
    ) -> None:
        approval_id = str(event.payload.get("approval_id") or event.payload.get("call_id") or "")
        if not approval_id:
            return
        surface_id = f"approval-{approval_id}"
        detail = event.payload.get("detail")
        detail = detail if isinstance(detail, dict) else {}
        command = str(detail.get("command") or detail.get("reason") or "")
        kind = str(event.payload.get("kind") or "tool")
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
        await self._persist_event(
            record,
            RuntimeEvent.create(
                EventType.A2UI_SURFACE_BEGIN,
                agent_id=record.agent_id,
                user_id="local-user",
                session_id=record.session_id,
                invocation_id=record.id,
                seq_id=0,
                trace_id=record.trace_id,
                payload={
                    "surface_id": surface_id,
                    "surface": {"components": components},
                },
            ),
            on_event=on_event,
        )
        await self._persist_event(
            record,
            RuntimeEvent.create(
                EventType.A2UI_INTERACTION,
                agent_id=record.agent_id,
                user_id="local-user",
                session_id=record.session_id,
                invocation_id=record.id,
                seq_id=0,
                trace_id=record.trace_id,
                payload={
                    "surface_id": surface_id,
                    "interaction_id": approval_id,
                    "kind": "approval",
                    "input_schema": {
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
            ),
            on_event=on_event,
        )

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


def project_runtime_event(event: RuntimeEvent) -> tuple[str, dict[str, Any]]:
    """Project the canonical RuntimeEvent into Studio's persisted event view."""

    payload = dict(event.payload)
    event_type = event.event_type
    if event_type == EventType.RUN_STARTED:
        projected = "run.started"
    elif event_type == EventType.RUN_PROGRESS:
        projected = str(payload.get("native_event") or "run.progress")
        native_data = payload.get("native_data")
        if isinstance(native_data, dict):
            payload = dict(native_data)
    elif event_type in {EventType.REASONING_DELTA, EventType.REASONING_COMPLETED}:
        projected = (
            "thinking.delta" if event_type == EventType.REASONING_DELTA else "thinking.completed"
        )
    elif event_type == EventType.TEXT_DELTA:
        projected = "message.delta" if event.phase == "final_answer" else "thinking.delta"
    elif event_type == EventType.TEXT_COMPLETED:
        projected = "message.completed" if event.phase == "final_answer" else "thinking.completed"
    elif event_type == EventType.TOOL_CALL_BEGIN:
        projected = "command.started" if payload.get("name") == "codex.command" else "tool.started"
        if projected == "command.started":
            raw_args = payload.get("args")
            args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}
            payload = {
                "callId": str(payload.get("call_id") or ""),
                "command": str(args.get("command") or ""),
                "cwd": str(args.get("cwd") or ""),
                "commandActions": args.get("command_actions") or [],
            }
        else:
            payload = {
                "callId": str(payload.get("call_id") or ""),
                "tool": str(payload.get("name") or ""),
                "args": payload.get("args"),
            }
    elif event_type == EventType.TOOL_CALL_END:
        projected = (
            "command.completed" if payload.get("name") == "codex.command" else "tool.completed"
        )
        if projected == "command.completed":
            raw_result = payload.get("result")
            result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
            payload = {
                "callId": str(payload.get("call_id") or ""),
                "status": str(result.get("status") or ""),
                "exitCode": result.get("exit_code"),
                "durationMs": result.get("duration_ms"),
                "output": str(result.get("output") or ""),
            }
        else:
            raw_tool_result = payload.get("result")
            tool_result: dict[str, Any] = (
                raw_tool_result if isinstance(raw_tool_result, dict) else {}
            )
            tool_status = str(tool_result.get("status") or "")
            if tool_result.get("error"):
                tool_status = "failed"
            payload = {
                "callId": str(payload.get("call_id") or ""),
                "tool": str(payload.get("name") or ""),
                "status": tool_status,
                "durationMs": tool_result.get("duration_ms"),
                "output": str(tool_result.get("output") or ""),
                **({"error": str(tool_result["error"])} if tool_result.get("error") else {}),
            }
    elif event_type == EventType.RUN_COMPLETED:
        projected = "run.completed"
    elif event_type == EventType.RUN_FAILED:
        projected = "run.failed"
    elif event_type == EventType.RUN_CANCELED:
        projected = "run.cancelled"
    elif event_type == EventType.RUN_INTERRUPTED:
        projected = (
            "run.paused" if str(payload.get("status") or "") == "paused" else "run.interrupted"
        )
    elif event_type in {
        EventType.A2UI_SURFACE_BEGIN,
        EventType.A2UI_SURFACE_UPDATE,
        EventType.A2UI_SURFACE_END,
        EventType.A2UI_INTERACTION,
        EventType.A2UI_ACTION,
    }:
        projected = str(event_type)
        surface_id = str(payload.get("surface_id") or payload.get("surfaceId") or "")
        if surface_id:
            payload["surfaceId"] = surface_id
        interaction_id = str(payload.get("interaction_id") or payload.get("interactionId") or "")
        if interaction_id:
            payload["interactionId"] = interaction_id
        schema = payload.get("input_schema", payload.get("inputSchema"))
        if isinstance(schema, dict):
            payload["inputSchema"] = schema
        operations = project_a2ui_operations(event_type, payload)
        if operations:
            payload["a2uiOperations"] = operations
    else:
        projected = str(event_type)
    payload["runtimeEvent"] = event.to_dict()
    return projected, payload


__all__ = ["StudioRunService", "StudioRunSpec", "project_runtime_event"]
