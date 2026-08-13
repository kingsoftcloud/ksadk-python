"""Studio Run persistence on top of the canonical RuntimeExecutor."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ksadk.agui.a2ui_projection import project_a2ui_operations
from ksadk.events.runtime_event import EventType, RuntimeEvent
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
_STUDIO_LOCAL_USER_ID = "local-user"


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
            if prepared_turn is not None:
                conversation_request["prepared_turn"] = asdict(prepared_turn)
            request = StartRequest(
                input=runtime_input if runtime_input is not None else user_input,
                user_id=_STUDIO_LOCAL_USER_ID,
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
                            self._persist_approval_surface(record, event, on_event=on_event)
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
                resumed = self.event_store.append(
                    run_id,
                    "run.resumed",
                    {"runId": run_id, "runtimeHandle": record.runtime_handle},
                )
                if on_event is not None:
                    on_event(resumed)
                self.event_store.save(record)
            record.output = final_text or streamed_final
            await self._finalize_via_shared(record, spec, user_input)
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
                user_id=_STUDIO_LOCAL_USER_ID,
                session_id=session,
                invocation_id=handle.run_id if handle is not None else run_id,
                seq_id=len(self.event_store.events(run_id)) + 1,
                payload={"status": "cancelled", "cancel_result": cancel_result},
            )
            persist(cancelled)
            raise
        except Exception as exc:  # noqa: BLE001
            record.status = RunStatus.FAILED
            record.error = {"code": "RUNTIME_RUN_FAILED", "message": str(exc)}
            failure = RuntimeEvent.create(
                EventType.RUN_FAILED,
                agent_id=spec.agent_id,
                user_id=_STUDIO_LOCAL_USER_ID,
                session_id=session,
                invocation_id=handle.run_id if handle is not None else run_id,
                seq_id=len(self.event_store.events(run_id)) + 1,
                payload={"status": "failed", "error": str(exc)},
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

    async def _finalize_via_shared(
        self,
        record: RunRecord,
        spec: StudioRunSpec,
        user_input: str,
    ) -> None:
        """Finalize hosted turns through the shared PCM lifecycle.

        Usage 回填始终执行（不依赖 memory_write_rollout）；
        Memory flush 由 memory_write_rollout 决定（在 finalize_hosted_turn 内部门控）。
        """
        if record.status != RunStatus.COMPLETED:
            return
        try:
            from types import SimpleNamespace

            from ksadk.runtime.hosted_finalizer import FinalizeContext, finalize_hosted_turn

            turn_event = SimpleNamespace(
                author="user",
                event_type="user_message",
                text=user_input,
                seq_id=1,
                id=f"{record.id}:user",
            )
            usage_dict = (
                record.usage.model_dump(by_alias=False, mode="json")
                if record.usage.reported and hasattr(record.usage, "model_dump")
                else None
            )
            await finalize_hosted_turn(
                FinalizeContext(
                    session_id=record.session_id,
                    invocation_id=record.id,
                    user_id=_STUDIO_LOCAL_USER_ID,
                    context_plan=record.context_plan,
                    shadow_context_plan=None,
                    usage=usage_dict,
                    runtime_type=record.runtime_type,
                    prompt_integration_mode=str(
                        spec.request_config.get("prompt_integration_mode") or ""
                    ),
                    memory_write_rollout=str(spec.request_config.get("memory_write_rollout") or ""),
                    memory_enabled=bool(spec.request_config.get("memory_enabled", False)),
                    memory_recall_enabled=bool(
                        spec.request_config.get("memory_recall_enabled", True)
                    ),
                    memory_write_mode=str(
                        spec.request_config.get("memory_write_mode") or "candidate"
                    ),
                    flush_before_compaction=bool(
                        spec.request_config.get("flush_before_compaction", True)
                    ),
                    provider_ref=str(spec.request_config.get("provider_ref") or "local-default"),
                    emit_event=lambda d: self.event_store.append(
                        record.id, d.get("type", "memory.event"), d
                    ),
                    session_events=[turn_event],
                ),
            )
        except Exception:  # noqa: BLE001 - finalization must not break a Studio run
            return

    async def _capture_pcm_evidence(
        self,
        record: RunRecord,
        spec: StudioRunSpec,
        user_input: str,
    ) -> Any | None:
        """Prepare once and persist the exact PCM evidence consumed by the adapter."""
        try:
            from ksadk.conversations.runtime_preparation import build_run_input
            from ksadk.sessions.in_memory import InMemorySessionService

            temporary_sessions = InMemorySessionService()
            await temporary_sessions.create_session(
                agent_id=spec.agent_id,
                user_id=_STUDIO_LOCAL_USER_ID,
                session_id=record.session_id,
            )
            cfg = dict(spec.request_config or {})
            prepared = await build_run_input(
                agent_id=spec.agent_id,
                user_id=_STUDIO_LOCAL_USER_ID,
                session_id=record.session_id,
                messages=self._conversation_messages(
                    spec.agent_id,
                    record.session_id,
                    user_input,
                ),
                model=spec.model,
                instructions=str(cfg.get("instructions") or cfg.get("base_instructions") or ""),
                agent_system=str(cfg.get("agent_system") or ""),
                agent_task=str(cfg.get("agent_task") or ""),
                prompt_integration_mode=str(cfg.get("prompt_integration_mode") or ""),
                context_engine_rollout=str(cfg.get("context_engine_rollout") or "") or None,
                memory_recall_enabled=cfg.get("memory_recall_enabled"),
                memory_write_rollout=str(cfg.get("memory_write_rollout") or "") or None,
                runtime_type=spec.launch_context.runtime_type,
                deployment_mode=spec.launch_context.deployment_mode,
                invocation_id=record.id,
                session_service_provider=lambda: temporary_sessions,
                agent_max_input_tokens=cfg.get("max_input_tokens"),
                agent_reserve_output_tokens=cfg.get("reserve_output_tokens"),
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
                "integrationMode": shadow.get("integration_mode"),
                "accountingAccuracy": shadow.get("accounting_accuracy"),
                "promptOwner": shadow.get("prompt_owner"),
                "runtimeType": shadow.get("runtime_type"),
                "deploymentMode": shadow.get("deployment_mode"),
                "capabilityHash": shadow.get("capability_hash"),
                "tokensByKind": shadow.get("tokens_by_kind", {}),
                "plannedInputTokens": shadow.get("planned_input_tokens"),
            }
            record.working_state = prepared.working_state
            self.event_store.save(record)
            return prepared
        except Exception:  # noqa: BLE001 - evidence collection is best effort
            return None

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
