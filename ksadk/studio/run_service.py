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
)
from ksadk.events.content import (
    ContentSnapshot,
    DataContent,
    TextContent,
    ToolCallContent,
    ToolResultContent,
)
from ksadk.runtime import (
    CONVERSATION_PREPROCESSING_METADATA_KEY,
    PauseResult,
    ResumePayload,
    ResumeTarget,
    RuntimeExecutor,
    RuntimeLaunchContext,
    StartRequest,
)
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
    ) -> None:
        self.workspace = workspace
        self.executor = executor
        self.event_store = event_store or RunEventStore(workspace)
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
        self.event_store.create(record)
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

        started = time.monotonic()
        record.status = RunStatus.RUNNING
        record.started_at = datetime.now(timezone.utc)
        self.event_store.save(record)

        def persist(runtime_event: RuntimeEvent) -> RunEvent:
            event_type, data = project_runtime_event(runtime_event)
            stored = self.event_store.append(record.id, event_type, data)
            if on_event is not None:
                on_event(stored)
            return stored

        handle = None
        final_text = ""
        streamed_final = ""
        runtime_duration_ms: int | None = None
        item_phases: dict[tuple[str, str], str | None] = {}
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
                    persist(event)
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
                        if event.op == "replace":
                            streamed_final = text
                        else:
                            streamed_final += text
                    elif (
                        isinstance(event, ItemCompleted)
                        and event.item_kind == "message"
                        and item_phases.get(
                            (event.scope_id, event.item_id), "final_answer"
                        )
                        == "final_answer"
                    ):
                        for part in event.snapshot.parts:
                            if isinstance(part, TextContent):
                                final_text = part.text
                                break
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
                resumed = self.event_store.append(
                    run_id,
                    "run.resumed",
                    {"runId": run_id, "runtimeHandle": record.runtime_handle},
                )
                if on_event is not None:
                    on_event(resumed)
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
            persist(cancelled)
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
            persist(failure)
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
        return record

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
        prior = [
            event
            for event in self.event_store.events(run_id)
            if event.type == "a2ui.action"
            and str(event.data.get("interactionId") or event.data.get("interaction_id") or "")
            == interaction_id
        ]
        if prior:
            return {"runId": run_id, "interactionId": interaction_id, "status": "resolved"}

        interaction = next(
            (
                event
                for event in reversed(self.event_store.events(run_id))
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

        resolved = self.event_store.append(
            run_id,
            "approval.resolved" if kind == "approval" else "interaction.resolved",
            {
                "runId": run_id,
                "interactionId": interaction_id,
                "callId": interaction_id,
                "name": name,
                "data": dict(data or {}),
            },
        )
        action = self.event_store.append(
            run_id,
            "a2ui.action",
            {
                "runId": run_id,
                "surfaceId": str(
                    interaction.data.get("surfaceId") or interaction.data.get("surface_id") or ""
                ),
                "interactionId": interaction_id,
                "actionId": f"action-{interaction_id}",
                "name": name,
                "data": dict(data or {}),
            },
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


def project_runtime_event(event: RuntimeEvent) -> tuple[str, dict[str, Any]]:
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
    payload["runtimeEvent"] = dump_runtime_event(event)
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
