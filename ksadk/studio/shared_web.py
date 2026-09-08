"""Project AgentKit Studio runs onto Responses and compatibility API contracts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ksadk.conversations.contracts import (
    APPROVAL_MODE_EXTENSION,
    COLLABORATION_MODE_EXTENSION,
    GOAL_OBJECTIVE_EXTENSION,
    ConversationAttachmentPart,
    ConversationInput,
    ConversationTextPart,
    validate_conversation_input,
)
from ksadk.sessions.base import Session
from ksadk.studio.contracts import OperationStatus, RunRecord, RunStatus
from ksadk.studio.errors import StudioError, not_found
from ksadk.studio.service import StudioService
from ksadk.tools.gateway import tool_approval_capability

_TERMINAL_OPERATIONS = {
    OperationStatus.SUCCEEDED,
    OperationStatus.FAILED,
    OperationStatus.CANCELLED,
    OperationStatus.INTERRUPTED,
}


class StudioSharedWebBridge:
    """Project Studio repositories onto Responses and legacy action surfaces."""

    def __init__(self, studio: StudioService) -> None:
        self.studio = studio
        self._operations_by_invocation: dict[str, str] = {}
        self._response_runs: dict[str, str] = {}
        self._run_ids_by_invocation: dict[str, str] = {}

    def resolve_agent_id(self, requested: str | None = None) -> str:
        if requested:
            self.studio.agent_detail(requested)
            return requested
        agents = self.studio.list_agents(limit=1)
        if not agents:
            raise not_found("agent", "")
        return agents[0].metadata.id

    def bootstrap(self, agent_id: str) -> dict[str, Any]:
        draft = self._draft(agent_id)
        model = self._model_descriptor(agent_id)
        return {
            "Agent": {
                "AgentId": agent_id,
                "Name": draft.metadata.name,
                "Framework": self.studio.agent_runtime_type(agent_id),
            },
            "AccessMode": "Owner",
            "ApiFormats": ["responses"],
            "Model": model,
            "Capabilities": {
                "HostedChat": {
                    "Enabled": True,
                    "ApiFormats": ["responses"],
                    "PreferredTransport": "responses",
                    "Transports": [
                        {
                            "Protocol": "responses",
                            "Runtime": "agentkit-studio",
                            "Endpoint": "/agentengine/api/v1/RunAgent",
                            "Version": "v1",
                            "Capabilities": {
                                "A2UI": True,
                                "Interrupt": True,
                                "Cancel": True,
                            },
                        }
                    ],
                },
                "RunLifecycle": {
                    "Enabled": True,
                    "Resume": True,
                    "Abort": True,
                    "Checkpoints": False,
                    "CheckpointResume": False,
                    "CheckpointResumePreview": False,
                },
                "ApprovalPolicy": tool_approval_capability(),
                "InteractionV1": True,
                "WorkspaceFiles": {"Enabled": False},
                "NativeDashboard": {"Enabled": False},
                "NativeTerminal": {"Enabled": False},
                "Thinking": False,
                "ContextCompaction": self.studio.is_codex_agent(agent_id),
            },
        }

    def list_models(self, agent_id: str) -> dict[str, Any]:
        models = self._model_descriptors(agent_id)
        model = self._model_descriptor(agent_id, models)
        return {
            "Models": models,
            "Current": model["id"],
            "Source": "agentkit-studio",
        }

    def select_model(self, agent_id: str, requested: str | None = None) -> str:
        """Resolve and authorize the actual model used by the next turn."""

        return self._select_model(agent_id, str(requested or ""))

    async def list_sessions(
        self,
        agent_id: str,
        *,
        page: int = 1,
        page_size: int = 30,
    ) -> dict[str, Any]:
        sessions = await self._sessions(agent_id)
        safe_page = max(1, page)
        safe_size = min(100, max(1, page_size))
        start = (safe_page - 1) * safe_size
        return {
            "Sessions": sessions[start : start + safe_size],
            "Total": len(sessions),
            "Page": safe_page,
            "PageSize": safe_size,
        }

    async def create_session(self, agent_id: str) -> dict[str, Any]:
        self._draft(agent_id)
        session_id = f"ses_{uuid4().hex}"
        session = await self.studio.session_service.create_session(
            agent_id,
            "local-user",
            session_id,
        )
        return {"Session": self._session_metadata_record(session)}

    async def get_session(self, session_id: str) -> dict[str, Any]:
        runs = self.studio.event_store.list_runs(session_id=session_id)
        if runs:
            return {"Session": self._session_record(runs)}
        session = await self.studio.session_service.get_session_metadata(session_id)
        if session is None:
            raise not_found("session", session_id)
        return {"Session": self._session_metadata_record(session)}

    async def compact_session(self, agent_id: str, session_id: str) -> dict[str, Any]:
        from ksadk.codex.runtime import CodexRuntimeAdapter

        runs = self.studio.event_store.list_runs(session_id=session_id, agent_id=agent_id)
        if not runs:
            raise not_found("session", session_id)
        latest = runs[-1]
        if latest.runtime_type != "codex":
            raise StudioError("COMPACTION_UNSUPPORTED", "此运行时尚未提供手动压缩", status_code=409)
        key = (agent_id, session_id)
        service = self.studio.run_service
        if key in service._active_sessions or any(self._active_status(run.status) for run in runs):
            raise StudioError(
                "SESSION_RUN_ACTIVE", "请等待当前运行完成后压缩上下文", status_code=409
            )
        thread_id = str((latest.runtime_handle.get("native_ref") or {}).get("thread_id") or "")
        if not thread_id:
            raise StudioError(
                "CONTEXT_UNAVAILABLE", "此会话尚无可压缩的原生上下文", status_code=409
            )
        service._active_sessions.add(key)
        adapter = None
        try:
            spec = self.studio.resolve_run_spec(latest.build_id, model=latest.model or None)
            adapter = self.studio.runtime_executor.create_adapter(spec.launch_context)
            if not isinstance(adapter, CodexRuntimeAdapter):
                raise StudioError(
                    "COMPACTION_UNSUPPORTED", "此运行时尚未提供手动压缩", status_code=409
                )
            usage = await adapter.compact_session(thread_id, dict(spec.request_config))
            last = usage.get("last") or {}
            context_usage = {
                "used_tokens": last.get("totalTokens"),
                "source": "runtime",
                "model": latest.model,
            }
            self.studio.event_store.append(
                latest.id,
                "context.compaction.completed",
                {
                    "trigger": "manual",
                    "contextUsage": context_usage,
                },
            )
            return {"Status": "completed", "ContextUsage": context_usage}
        finally:
            try:
                if adapter is not None:
                    await adapter.close_all()
            finally:
                service._active_sessions.discard(key)

    async def delete_session(self, session_id: str) -> dict[str, Any]:
        await self.studio.delete_session(session_id)
        return {}

    async def list_messages(
        self,
        session_id: str,
        *,
        after_seq_id: int | None = None,
        before_seq_id: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        runs = self.studio.event_store.list_runs(session_id=session_id)
        messages: list[dict[str, Any]] = []
        sequence = 0
        for run in runs:
            sequence += 1
            messages.append(
                {
                    "MessageId": f"{run.id}:user",
                    "Role": "user",
                    "Content": {"text": run.input},
                    "Timestamp": self._timestamp(run.started_at),
                    "SeqId": sequence,
                    "InvocationId": run.id,
                }
            )
            sequence += 1
            messages.append(
                {
                    "MessageId": f"{run.id}:assistant",
                    "Role": "assistant",
                    "Content": {
                        # Run status is metadata, not an assistant reply. An
                        # empty pending reply lets canonical reasoning/tools
                        # restore without a synthetic status text shadowing them.
                        "text": (
                            run.output if self._active_status(run.status) else self._run_output(run)
                        )
                    },
                    "Timestamp": self._timestamp(run.completed_at or run.started_at),
                    "SeqId": sequence,
                    "InvocationId": run.id,
                    "Activities": await self._run_activities(run),
                }
            )

        latest_seq_id = sequence
        if after_seq_id is not None:
            messages = [item for item in messages if item["SeqId"] > after_seq_id]
        if before_seq_id is not None:
            messages = [item for item in messages if item["SeqId"] < before_seq_id]
        safe_limit = min(200, max(1, limit))
        has_more = len(messages) > safe_limit
        selected = messages[-safe_limit:]
        return {
            "Messages": selected,
            "LatestSeqId": latest_seq_id,
            "HasMore": has_more,
            "NextCursor": selected[0]["SeqId"] if has_more and selected else None,
        }

    async def list_session_events(self, session_id: str) -> dict[str, Any]:
        runs = self.studio.event_store.list_runs(session_id=session_id)
        events: list[dict[str, Any]] = []
        sequence = 0
        for run in runs:
            for event in await self.studio.run_service.events(run.id):
                sequence += 1
                events.append(
                    {
                        "SeqId": sequence,
                        "EventType": self._shared_event_type(event.type, event.data),
                        "InvocationId": run.id,
                        "Content": self._shared_event_content(event.data, run.id),
                        "Timestamp": self._timestamp(event.created_at),
                    }
                )
        return {
            "Events": events,
            "Total": len(events),
            "Offset": 0,
            "Limit": len(events),
        }

    @staticmethod
    def _shared_event_type(event_type: str, data: dict[str, Any]) -> str:
        native = data.get("runtimeEvent")
        if (
            event_type == "run.interrupted"
            and isinstance(native, dict)
            and native.get("interaction_id")
        ):
            # A resumable interaction is waiting, not a terminal interruption.
            return "run.waiting"
        return event_type

    @staticmethod
    def _shared_event_content(data: dict[str, Any], run_id: str) -> dict[str, Any]:
        """Use the public Studio run identity in shared-Web history projections.

        The persisted canonical event retains the adapter's native run ID.
        ListSessionMessages uses the Studio ID, so its event projection must
        use the same ID or hydration retains both copies of the answer.
        """
        native = data.get("runtimeEvent")
        if not isinstance(native, dict):
            return data
        return {**data, "runtimeEvent": {**native, "run_id": run_id}}

    def subscription_run_id(self, session_id: str, invocation_id: str) -> str:
        run_id = self._run_ids_by_invocation.get(invocation_id, invocation_id)
        record = self.studio.event_store.get(run_id)
        if record.session_id != session_id:
            raise not_found("run", invocation_id)
        return run_id

    async def subscribe_run_events(
        self,
        session_id: str,
        invocation_id: str,
        *,
        after_seq_id: int = 0,
    ) -> AsyncIterator[str]:
        run_id = self.subscription_run_id(session_id, invocation_id)
        # Match ListSessionEvents' session cursor without rereading every old
        # run on every live poll. Only the subscribed run can still grow.
        offset = 0
        for record in self.studio.event_store.list_runs(session_id=session_id):
            if record.id == run_id:
                break
            offset += len(self.studio.event_store.events(record.id))
        cursor = max(0, after_seq_id - offset)
        while True:
            for event in await self.studio.run_service.events(run_id, after=cursor):
                cursor = max(cursor, event.id)
                yield self._sse(
                    "message",
                    {
                        "SeqId": offset + event.id,
                        "SessionId": session_id,
                        "InvocationId": invocation_id,
                        "EventType": self._shared_event_type(event.type, event.data),
                        "Content": self._shared_event_content(event.data, run_id),
                        "Timestamp": self._timestamp(event.created_at),
                    },
                )
            record = self.studio.event_store.get(run_id)
            if not self._active_status(record.status):
                yield "event: done\ndata: [DONE]\n\n"
                return
            yield ": ping\n\n"
            await asyncio.sleep(0.25)

    def cancel_run(self, invocation_id: str) -> dict[str, Any]:
        operation_id = self._operations_by_invocation.get(invocation_id)
        if operation_id:
            self.studio.operations.cancel(operation_id)
        return {"InvocationId": invocation_id, "Cancelled": bool(operation_id)}

    async def pause_run(self, invocation_id: str) -> dict[str, Any]:
        run_id = self._run_ids_by_invocation.get(invocation_id)
        if not run_id:
            raise StudioError("RUN_NOT_READY", "运行尚未创建，请稍后重试", status_code=409)
        return await self.studio.run_service.pause_run(run_id)

    async def resume_run(self, invocation_id: str) -> dict[str, Any]:
        run_id = self._run_ids_by_invocation.get(invocation_id)
        if not run_id:
            raise StudioError("RUN_NOT_FOUND", "未找到可继续的运行", status_code=404)
        return await self.studio.run_service.resume_run(run_id)

    def response_session_id(self, response_id: str) -> str:
        run_id = self._response_runs.get(response_id, response_id)
        return self.studio.event_store.get(run_id).session_id

    async def stream_run(
        self, payload: dict[str, Any], *, shared_ui: bool = False
    ) -> AsyncIterator[str]:
        agent_id = self.resolve_agent_id(str(payload.get("AgentId") or "") or None)
        session_id = str(payload.get("SessionId") or f"ses_{uuid4().hex}")
        invocation_id = str(payload.get("InvocationId") or f"resp_{uuid4().hex}")
        prompt = self._input_text(payload)
        runtime_input = self._runtime_input(payload)
        model = self._select_model(agent_id, str(payload.get("Model") or ""))
        model_explicit = bool(payload.get("ModelExplicit", str(payload.get("Model") or "")))
        approval_mode, collaboration_mode, goal_objective, reasoning_effort = (
            self._request_controls(payload)
        )
        try:
            build = await self._ensure_build(agent_id)
            self._validate_conversation_turn(
                build_id=build.id,
                session_id=session_id,
                invocation_id=invocation_id,
                prompt=prompt,
                runtime_input=runtime_input,
                model=model if model_explicit else "",
                approval_mode=approval_mode,
                collaboration_mode=collaboration_mode,
                goal_objective=goal_objective,
                reasoning_effort=reasoning_effort,
            )
        except StudioError as exc:
            # StreamingResponse starts the HTTP response before advancing this
            # generator.  Preflight failures therefore belong in the stream;
            # raising here would turn an actionable Studio error into Starlette's
            # "response already started" RuntimeError.
            yield self._failed_sse(invocation_id, exc.message)
            return
        except Exception:
            yield self._failed_sse(invocation_id, "本地 Agent 运行失败")
            return
        execution = asyncio.create_task(
            self._execute_run(
                build=build,
                agent_id=agent_id,
                session_id=session_id,
                invocation_id=invocation_id,
                prompt=prompt,
                runtime_input=runtime_input,
                model=model,
                approval_mode=approval_mode,
                collaboration_mode=collaboration_mode,
                goal_objective=goal_objective,
                reasoning_effort=reasoning_effort,
            )
        )

        def release_operation(task: asyncio.Task[RunRecord]) -> None:
            self._operations_by_invocation.pop(invocation_id, None)
            if not task.cancelled():
                task.exception()

        execution.add_done_callback(release_operation)

        yield self._sse(
            "response.created",
            {
                "type": "response.created",
                "response": self._response_shell(invocation_id, model=model),
            },
        )
        try:
            yield self._sse(
                "response.in_progress",
                {
                    "type": "response.in_progress",
                    "response": self._response_shell(invocation_id, model=model),
                },
            )
            seen_events: set[tuple[str, int]] = set()
            emitted_text = ""
            idle_polls = 0
            while not execution.done():
                projected = await self._project_response_events(
                    session_id=session_id,
                    agent_id=agent_id,
                    invocation_id=invocation_id,
                    seen=seen_events,
                )
                if projected:
                    idle_polls = 0
                    for event_name, event_payload in projected:
                        if event_name == "response.output_text.delta":
                            emitted_text += str(event_payload.get("delta") or "")
                        yield self._sse(event_name, event_payload)
                        if (
                            shared_ui
                            and event_name == "a2ui.interaction"
                            and event_payload.get("kind") in {"form", "structured_input"}
                        ):
                            # The shared UI restores the durable Interaction/v1
                            # request, then subscribes to this same live run.
                            # Leave the execution attached while input is pending.
                            return
                else:
                    idle_polls += 1
                    if idle_polls >= 20:
                        idle_polls = 0
                        yield ": keep-alive\n\n"
                await asyncio.sleep(0.05)
            run = await execution
            for event_name, event_payload in await self._project_response_events(
                session_id=session_id,
                agent_id=agent_id,
                invocation_id=invocation_id,
                seen=seen_events,
            ):
                if event_name == "response.output_text.delta":
                    emitted_text += str(event_payload.get("delta") or "")
                yield self._sse(event_name, event_payload)
            remaining = (
                run.output[len(emitted_text) :] if run.output.startswith(emitted_text) else ""
            )
            if remaining:
                yield self._response_delta_sse(
                    "response.output_text.delta",
                    invocation_id=invocation_id,
                    delta=remaining,
                )
            yield self._sse(
                "response.completed",
                {
                    "type": "response.completed",
                    "response": self._response_payload(
                        run,
                        model=model,
                        response_id=invocation_id,
                    ),
                },
            )
        except StudioError as exc:
            yield self._failed_sse(invocation_id, exc.message)
        except Exception:
            yield self._failed_sse(invocation_id, "本地 Agent 运行失败")
        finally:
            if execution.done():
                self._operations_by_invocation.pop(invocation_id, None)

    async def invoke_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run once and return an OpenAI Responses-compatible JSON object."""

        agent_id = self.resolve_agent_id(str(payload.get("AgentId") or "") or None)
        session_id = str(payload.get("SessionId") or f"ses_{uuid4().hex}")
        invocation_id = str(payload.get("InvocationId") or f"resp_{uuid4().hex}")
        model = self._select_model(agent_id, str(payload.get("Model") or ""))
        model_explicit = bool(payload.get("ModelExplicit", str(payload.get("Model") or "")))
        approval_mode, collaboration_mode, goal_objective, reasoning_effort = (
            self._request_controls(payload)
        )
        build = await self._ensure_build(agent_id)
        prompt = self._input_text(payload)
        runtime_input = self._runtime_input(payload)
        self._validate_conversation_turn(
            build_id=build.id,
            session_id=session_id,
            invocation_id=invocation_id,
            prompt=prompt,
            runtime_input=runtime_input,
            model=model if model_explicit else "",
            approval_mode=approval_mode,
            collaboration_mode=collaboration_mode,
            goal_objective=goal_objective,
            reasoning_effort=reasoning_effort,
        )
        try:
            run = await self._execute_run(
                build=build,
                agent_id=agent_id,
                session_id=session_id,
                invocation_id=invocation_id,
                prompt=prompt,
                runtime_input=runtime_input,
                model=model,
                approval_mode=approval_mode,
                collaboration_mode=collaboration_mode,
                goal_objective=goal_objective,
                reasoning_effort=reasoning_effort,
            )
            return self._response_payload(
                run,
                model=model,
                response_id=invocation_id,
            )
        finally:
            self._operations_by_invocation.pop(invocation_id, None)

    async def _execute_run(
        self,
        *,
        build: Any = None,
        agent_id: str,
        session_id: str,
        invocation_id: str,
        prompt: str,
        runtime_input: Any,
        model: str,
        approval_mode: str = "",
        collaboration_mode: str = "",
        goal_objective: str = "",
        reasoning_effort: str = "",
    ) -> RunRecord:
        if build is None:
            build = await self._ensure_build(agent_id)
        bound_agent = str(getattr(build, "agent_name", None) or getattr(build, "agent_id", ""))
        if bound_agent != agent_id:
            raise StudioError(
                "CONVERSATION_BUILD_MISMATCH",
                "会话 Build 不属于当前 Agent",
                status_code=409,
            )

        def observe(event: Any) -> None:
            if event.type == "run.created":
                run_id = str(event.data.get("runId") or "")
                if run_id:
                    self._run_ids_by_invocation[invocation_id] = run_id

        operation = self.studio.submit_studio_run(
            build.id,
            prompt,
            session_id=session_id,
            model=model,
            approval_mode=approval_mode or None,
            collaboration_mode=collaboration_mode or None,
            goal_objective=goal_objective or None,
            reasoning_effort=reasoning_effort or None,
            runtime_input=runtime_input or None,
            idempotency_key=f"responses:{invocation_id}",
            on_event=observe,
        )
        self._operations_by_invocation[invocation_id] = operation.id
        while True:
            current = self.studio.operations.get(operation.id)
            if current.status in _TERMINAL_OPERATIONS:
                break
            await asyncio.sleep(0.05)
        if current.status != OperationStatus.SUCCEEDED:
            message = str((current.error or {}).get("message") or "Agent 运行失败")
            raise StudioError("RUN_FAILED", message, status_code=500)
        run = self.studio.event_store.get(current.resource_id)
        if run.status != RunStatus.COMPLETED:
            raise StudioError("RUN_FAILED", self._run_output(run), status_code=500)
        self._response_runs[invocation_id] = run.id
        return run

    @staticmethod
    def _response_shell(response_id: str, *, model: str) -> dict[str, Any]:
        return {
            "id": response_id,
            "object": "response",
            "status": "in_progress",
            "model": model,
            "output": [],
        }

    async def _project_response_events(
        self,
        *,
        session_id: str,
        agent_id: str,
        invocation_id: str,
        seen: set[tuple[str, int]],
    ) -> list[tuple[str, dict[str, Any]]]:
        projected: list[tuple[str, dict[str, Any]]] = []
        current_run_id = self._run_ids_by_invocation.get(invocation_id)
        if not current_run_id:
            return projected
        for run in self.studio.event_store.list_runs(session_id=session_id):
            if run.agent_id != agent_id or run.id != current_run_id:
                continue
            events = await self.studio.run_service.events(run.id)
            starts = {
                str(event.data.get("callId") or event.data.get("call_id") or ""): event.data
                for event in events
                if event.type in {"command.started", "tool.started", "tool.requested"}
            }
            for event in events:
                key = (run.id, event.id)
                if key in seen:
                    continue
                seen.add(key)
                if event.type in {"message.delta", "thinking.delta"}:
                    delta = str(event.data.get("text") or event.data.get("delta") or "")
                    if not delta:
                        continue
                    event_name = (
                        "response.output_text.delta"
                        if event.type == "message.delta"
                        else "response.reasoning_summary_text.delta"
                    )
                    projected.append(
                        (
                            event_name,
                            self._response_delta_payload(
                                event_name,
                                invocation_id=invocation_id,
                                delta=delta,
                            ),
                        )
                    )
                    continue
                if event.type.startswith("a2ui."):
                    projected.append(
                        (
                            event.type,
                            {
                                "type": event.type,
                                "runId": run.id,
                                **event.data,
                            },
                        )
                    )
                    continue
                if event.type == "run.paused":
                    projected.append(
                        (
                            "response.paused",
                            {
                                "type": "response.paused",
                                "response_id": invocation_id,
                                "runId": run.id,
                            },
                        )
                    )
                    continue
                if event.type == "run.resumed":
                    projected.append(
                        (
                            "response.resumed",
                            {
                                "type": "response.resumed",
                                "response_id": invocation_id,
                                "runId": run.id,
                            },
                        )
                    )
                    continue
                if event.type == "approval.resolved":
                    projected.append(
                        (
                            "response.ksadk.approval_resolved",
                            {
                                "type": "response.ksadk.approval_resolved",
                                "approvalRequestId": event.data.get("approvalId")
                                or event.data.get("interactionId"),
                                "decision": event.data.get("decision")
                                or event.data.get("name")
                                or "approved",
                                "revision": event.data.get("revision") or 2,
                                "runId": run.id,
                            },
                        )
                    )
                    continue
                item_event = self._response_item_event(event.type, event.data, starts)
                if item_event is not None:
                    projected.append(item_event)
        return projected

    @staticmethod
    def _response_item_event(
        event_type: str,
        data: dict[str, Any],
        starts: dict[str, dict[str, Any]],
    ) -> tuple[str, dict[str, Any]] | None:
        call_id = str(data.get("callId") or data.get("call_id") or "")
        started = starts.get(call_id, {})
        done = event_type in {
            "command.completed",
            "tool.completed",
            "command.failed",
            "tool.failed",
        }
        if event_type.startswith("command."):
            command = str(data.get("command") or started.get("command") or "执行命令")
            item = {
                "id": call_id or f"shell_{uuid4().hex}",
                "call_id": call_id,
                "type": "shell_call",
                "status": (
                    "failed"
                    if event_type.endswith("failed") or data.get("exitCode") not in {None, 0}
                    else "completed"
                    if done
                    else "in_progress"
                ),
                "action": {
                    "commands": [command],
                    "cwd": data.get("cwd") or started.get("cwd") or "",
                },
                "exit_code": data.get("exitCode"),
                "output": data.get("output") or "",
            }
        elif event_type.startswith("tool."):
            name = str(data.get("tool") or data.get("name") or started.get("tool") or "调用工具")
            args = data.get("args", started.get("args"))
            item = {
                "id": call_id or f"tool_{uuid4().hex}",
                "call_id": call_id,
                "type": "function_call",
                "name": name,
                "arguments": json.dumps(args, ensure_ascii=False) if args is not None else "",
                "status": (
                    "failed"
                    if event_type.endswith("failed")
                    else "completed"
                    if done
                    else "in_progress"
                ),
                "output": data.get("output") or data.get("result") or "",
            }
        elif event_type == "approval.requested":
            approval_id = str(data.get("approvalId") or data.get("interactionId") or call_id or "")
            detail = data.get("detail")
            arguments = detail if isinstance(detail, dict) else {"detail": detail}
            item = {
                "id": approval_id or f"approval_{uuid4().hex}",
                "call_id": call_id,
                "type": "mcp_approval_request",
                "name": str(data.get("kind") or started.get("tool") or "人工确认"),
                "arguments": json.dumps(arguments, ensure_ascii=False),
                "run_id": str(data.get("runId") or ""),
                "status": "in_progress",
            }
        else:
            return None
        response_event = "response.output_item.done" if done else "response.output_item.added"
        return response_event, {"type": response_event, "item": item}

    @classmethod
    def _response_delta_sse(
        cls,
        event_name: str,
        *,
        invocation_id: str,
        delta: str,
    ) -> str:
        payload = cls._response_delta_payload(
            event_name,
            invocation_id=invocation_id,
            delta=delta,
        )
        return cls._sse(event_name, payload)

    @staticmethod
    def _response_delta_payload(
        event_name: str,
        *,
        invocation_id: str,
        delta: str,
    ) -> dict[str, Any]:
        return {
            "type": event_name,
            "item_id": f"msg_{invocation_id}",
            "output_index": 0,
            "content_index": 0,
            "delta": delta,
        }

    @staticmethod
    def _response_payload(
        run: RunRecord,
        *,
        model: str,
        response_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "id": response_id or run.id,
            "object": "response",
            "status": "completed",
            "model": run.model or model,
            "output": [
                {
                    "id": f"msg_{response_id or run.id}",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": run.output,
                            "annotations": [],
                        }
                    ],
                }
            ],
            "usage": {
                "input_tokens": run.usage.input_tokens,
                "output_tokens": run.usage.output_tokens,
                "total_tokens": run.usage.total_tokens,
            },
            "metadata": {
                "session_id": run.session_id,
                "trace_id": run.trace_id,
                "agent_id": run.agent_id,
                "runtime_run_id": run.id,
            },
        }

    async def _ensure_build(self, agent_id: str):
        return await self.studio.ensure_current_build(agent_id)

    def _validate_conversation_turn(
        self,
        *,
        build_id: str,
        session_id: str,
        invocation_id: str,
        prompt: str,
        runtime_input: Any,
        model: str,
        approval_mode: str,
        collaboration_mode: str,
        goal_objective: str,
        reasoning_effort: str,
    ) -> None:
        """Revalidate compatibility requests against the active Surface.

        `/v1/responses` and the legacy RunAgent action remain supported wire
        shapes, but neither may smuggle provider-only input past the shared
        ConversationInput contract.
        """

        surface = self.studio.conversation_surface(build_id, session_id=session_id)
        parts: list[ConversationTextPart | ConversationAttachmentPart] = [
            ConversationTextPart(text=prompt)
        ]
        for index, item in enumerate(runtime_input if isinstance(runtime_input, list) else []):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "")
            if kind in {"image", "input_image"}:
                media_type = self._data_url_media_type(
                    str(item.get("url") or item.get("image_url") or ""),
                    fallback="image/*",
                )
                parts.append(
                    ConversationAttachmentPart(
                        attachment_ref=f"attachment://inline/{invocation_id}/{index}",
                        media_type=media_type,
                        name=str(item.get("filename") or "image"),
                    )
                )
            elif kind == "input_file":
                parts.append(
                    ConversationAttachmentPart(
                        attachment_ref=f"attachment://inline/{invocation_id}/{index}",
                        media_type=self._data_url_media_type(
                            str(item.get("file_data") or item.get("file_url") or ""),
                            fallback="application/octet-stream",
                        ),
                        name=str(item.get("filename") or "attachment"),
                    )
                )
        conversation_input = ConversationInput(
            input_id=invocation_id,
            session_id=session_id,
            idempotency_key=f"responses:{invocation_id}",
            parts=tuple(parts),
            model_ref=model or None,
            reasoning=reasoning_effort or None,
            extensions={
                key: value
                for key, value in (
                    (APPROVAL_MODE_EXTENSION, approval_mode or None),
                    (COLLABORATION_MODE_EXTENSION, collaboration_mode or None),
                    (GOAL_OBJECTIVE_EXTENSION, goal_objective or None),
                )
                if value is not None
            },
        )
        try:
            validate_conversation_input(surface, conversation_input)
        except ValueError as exc:
            raise StudioError(
                "CONVERSATION_INPUT_UNSUPPORTED",
                "当前 Agent 不支持此会话输入",
                status_code=422,
                details={"reason": str(exc), "surfaceId": surface.surface_id},
            ) from exc

    @staticmethod
    def _data_url_media_type(value: str, *, fallback: str) -> str:
        if value.startswith("data:"):
            media_type = value[5:].split(";", 1)[0].strip().lower()
            if media_type:
                return media_type
        return fallback

    async def _sessions(self, agent_id: str) -> list[dict[str, Any]]:
        persisted = await self.studio.session_service.list_session_metadata(
            agent_id=agent_id,
            user_id="local-user",
        )
        records = {session.id: self._session_metadata_record(session) for session in persisted}
        grouped: dict[str, list[RunRecord]] = {}
        for run in self.studio.event_store.list_runs():
            if run.agent_id != agent_id:
                continue
            grouped.setdefault(run.session_id, []).append(run)
        for session_id, runs in grouped.items():
            records[session_id] = self._session_record(runs)
        ordered = list(records.values())
        ordered.sort(key=lambda item: item["UpdatedAt"], reverse=True)
        return ordered

    def _session_metadata_record(self, session: Session) -> dict[str, Any]:
        title = session.title
        if not title and session.first_prompt:
            title = self._short_title(session.first_prompt)
        return {
            "SessionId": session.id,
            "AgentId": session.agent_id,
            "UserId": session.user_id or "local-user",
            "Title": title or "新会话",
            "FirstPrompt": session.first_prompt,
            "LastPrompt": session.last_prompt,
            "CreatedAt": self._timestamp(session.created_at),
            "UpdatedAt": self._timestamp(session.updated_at),
            "ActiveRunStatus": "",
            "ActiveInvocationId": "",
            "TokenUsage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "turns": 0,
                "last_response_id": "",
            },
        }

    def _session_record(self, runs: list[RunRecord]) -> dict[str, Any]:
        ordered = sorted(
            runs,
            key=lambda run: (
                run.started_at or run.completed_at or datetime.min.replace(tzinfo=timezone.utc)
            ),
        )
        first = ordered[0]
        latest = ordered[-1]
        active = next((run for run in reversed(ordered) if self._active_status(run.status)), None)
        usage = {
            "input_tokens": sum(run.usage.input_tokens for run in ordered),
            "output_tokens": sum(run.usage.output_tokens for run in ordered),
            "total_tokens": sum(run.usage.total_tokens for run in ordered),
            "turns": len(ordered),
            "last_response_id": latest.id,
        }
        return {
            "SessionId": first.session_id,
            "AgentId": first.agent_id,
            "UserId": "local-user",
            "Title": self._short_title(first.input),
            "FirstPrompt": first.input,
            "LastPrompt": latest.input,
            "CreatedAt": self._timestamp(first.started_at),
            "UpdatedAt": self._timestamp(latest.completed_at or latest.started_at),
            "ActiveRunStatus": self._active_status(active.status) if active else "",
            "ActiveInvocationId": active.id if active else "",
            "TokenUsage": usage,
            "ContextUsage": self._context_usage(latest),
        }

    def _context_usage(self, latest: RunRecord) -> dict[str, Any] | None:
        if latest.runtime_type != "codex":
            return None
        for event in reversed(self.studio.event_store.events(latest.id)):
            if event.type == "context.compaction.completed":
                return event.data.get("contextUsage")
        if latest.runtime_type == "codex" and latest.usage.input_tokens > 0:
            return {
                "used_tokens": latest.usage.input_tokens,
                "source": "last_request",
                "model": latest.model,
            }
        return None

    def _model_descriptor(
        self, agent_id: str, models: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        if models is None:
            models = self._model_descriptors(agent_id)
        if self.studio.is_codex_agent(agent_id):
            default_model = self.studio.codex_manifests.load(agent_id).manifest.model
            return next(
                (item for item in models if item["id"] == default_model),
                models[0],
            )
        return models[0]

    def _model_descriptors(self, agent_id: str) -> list[dict[str, Any]]:
        draft = self._draft(agent_id)
        if self.studio.is_codex_agent(agent_id):
            manifest = self.studio.codex_manifests.load(agent_id).manifest
            # catalog.list 每次调用都要全量扫描（内建工具/持久化目录/Skill），
            # 按 allowed_models 逐个扫会成倍放大，这里一次取出后复用。
            catalog_models = self.studio.catalog.list(kind="model", limit=500)
            codex_descriptors = [
                self._model_descriptor_for_name(draft, model, catalog_models)
                for model in manifest.allowed_models
            ]
            return codex_descriptors

        binding_ids = list(draft.spec.bindings.model_profile_ids)
        if not binding_ids and draft.spec.bindings.model_profile_id:
            binding_ids = [draft.spec.bindings.model_profile_id]
        descriptors: list[dict[str, Any]] = []
        for binding_id in binding_ids:
            spec = self.studio.catalog.resolve_model(
                draft.spec.bindings.model_copy(
                    update={"model_profile_id": binding_id, "model_profile_ids": []}
                )
            )
            if spec is not None:
                descriptors.append(
                    self._model_descriptor_from_spec(
                        draft,
                        spec,
                        display_name=self.studio.catalog.get(binding_id).display_name,
                    )
                )
        if descriptors:
            default_id = draft.spec.bindings.model_profile_id
            if default_id and default_id in binding_ids:
                default_index = binding_ids.index(default_id)
                descriptors.insert(0, descriptors.pop(default_index))
            return descriptors
        if draft.spec.model is not None:
            return [self._model_descriptor_from_spec(draft, draft.spec.model)]
        return [self._unconfigured_model_descriptor(draft)]

    def _model_descriptor_for_name(
        self, draft, model_name: str, catalog_models: list | None = None
    ) -> dict[str, Any]:
        if catalog_models is None:
            catalog_models = self.studio.catalog.list(kind="model", limit=500)
        for descriptor in catalog_models:
            try:
                from ksadk.studio.contracts import ModelSpec

                spec = ModelSpec.model_validate(descriptor.contract)
            except ValueError:
                continue
            if spec.model == model_name:
                return self._model_descriptor_from_spec(
                    draft,
                    spec,
                    display_name=descriptor.display_name,
                )
        return {
            "id": model_name,
            "display_name": model_name,
            "source": "agentkit-studio",
            "input_budget_tokens": draft.spec.context.max_input_tokens,
            "max_output_tokens": 2048,
            "capabilities": {
                "function_calling": True,
                "structured_output": True,
            },
        }

    @staticmethod
    def _model_descriptor_from_spec(
        draft,
        spec,
        *,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        model_id = spec.model
        metadata = dict(spec.metadata or {})
        resolved_display_name = display_name or spec.model
        max_output_tokens = spec.parameters.max_tokens or 4096
        return {
            **metadata,
            "id": model_id,
            "display_name": resolved_display_name,
            "source": "agentkit-studio",
            "context_window_tokens": metadata.get("context_window_tokens"),
            "input_budget_tokens": draft.spec.context.max_input_tokens,
            "max_output_tokens": max_output_tokens,
            "capabilities": {
                **dict(metadata.get("capabilities") or {}),
                "function_calling": True,
                "structured_output": True,
            },
        }

    @staticmethod
    def _unconfigured_model_descriptor(draft) -> dict[str, Any]:
        model_id = str(
            draft.metadata.labels.get("agentkit.ksyun.com/model") or "unconfigured-model"
        )
        return {
            "id": model_id,
            "display_name": model_id if model_id != "unconfigured-model" else "未配置模型",
            "source": "agentkit-studio",
            "input_budget_tokens": draft.spec.context.max_input_tokens,
            "max_output_tokens": 2048,
            "capabilities": {
                "function_calling": True,
                "structured_output": True,
            },
        }

    def _select_model(self, agent_id: str, requested: str) -> str:
        models = self._model_descriptors(agent_id)
        allowed = [str(item["id"]) for item in models]
        default = str(self._model_descriptor(agent_id, models)["id"])
        selected = requested.strip() or default
        if selected not in allowed:
            raise StudioError(
                "MODEL_NOT_BOUND",
                "请求模型未绑定到当前 Agent Build",
                status_code=422,
                details={"model": selected, "allowedModels": allowed},
            )
        return selected

    def _draft(self, agent_id: str):
        return self.studio.agent_detail(agent_id)["draft"]

    async def _run_activities(self, run: RunRecord) -> list[dict[str, Any]]:
        activities: dict[str, dict[str, Any]] = {}
        for event in await self.studio.run_service.events(run.id):
            operations = (
                event.data.get("a2uiOperations")
                or event.data.get("a2ui_operations")
                or event.data.get("operations")
            )
            if not isinstance(operations, list):
                continue
            surface_id = str(
                event.data.get("surfaceId") or event.data.get("surface_id") or f"{run.id}-surface"
            )
            previous = activities.get(surface_id)
            # Snapshot updates may repeat createSurface; only the latest full
            # snapshot should become a history row. Delta-only batches append.
            reset = any(isinstance(op, dict) and "createSurface" in op for op in operations)
            previous_operations = (
                previous["Content"]["a2ui_operations"] if previous and not reset else []
            )
            activities[surface_id] = {
                "SeqId": event.id,
                "Type": event.type,
                "MessageId": f"{run.id}:assistant",
                "SurfaceId": surface_id,
                "Content": {"a2ui_operations": [*previous_operations, *operations]},
            }
        return list(activities.values())

    @staticmethod
    def _request_controls(payload: dict[str, Any]) -> tuple[str, str, str, str]:
        """Read turn controls from both legacy and shared-Web request shapes."""

        metadata = payload.get("Metadata") or payload.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        agentengine = metadata.get("agentengine")
        agentengine = agentengine if isinstance(agentengine, dict) else {}
        model_options = payload.get("ModelOptions") or payload.get("model_options")
        model_options = model_options if isinstance(model_options, dict) else {}

        approval_mode = (
            str(
                payload.get("ApprovalMode")
                or payload.get("approval_mode")
                or metadata.get("approval_mode")
                or metadata.get("approvalMode")
                or agentengine.get("tool_approval_mode")
                or agentengine.get("approval_mode")
                or ""
            )
            .strip()
            .lower()
        )
        collaboration_mode = (
            str(
                payload.get("CollaborationMode")
                or payload.get("collaboration_mode")
                or metadata.get("collaboration_mode")
                or metadata.get("collaborationMode")
                or agentengine.get("collaboration_mode")
                or ""
            )
            .strip()
            .lower()
        )
        goal_objective = str(
            payload.get("GoalObjective")
            or payload.get("goal_objective")
            or metadata.get("goal_objective")
            or metadata.get("goalObjective")
            or agentengine.get("goal_objective")
            or ""
        ).strip()
        reasoning_effort = (
            str(
                payload.get("ReasoningEffort")
                or payload.get("reasoning_effort")
                or model_options.get("reasoning_effort")
                or model_options.get("reasoningEffort")
                or ""
            )
            .strip()
            .lower()
        )
        return approval_mode, collaboration_mode, goal_objective, reasoning_effort

    @staticmethod
    def _input_text(payload: dict[str, Any]) -> str:
        sources = payload.get("ResponsesInput") or payload.get("Messages") or []
        if not isinstance(sources, list):
            raise StudioError(
                "RUN_INPUT_INVALID",
                "会话输入格式无效",
                status_code=422,
            )
        for message in reversed(sources):
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
            if not isinstance(content, list):
                continue
            texts = [
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and str(part.get("type") or "") in {"input_text", "text"}
            ]
            value = "\n".join(text.strip() for text in texts if text.strip())
            if value:
                return value
        raise StudioError(
            "RUN_INPUT_REQUIRED",
            "请输入消息后再发送",
            status_code=422,
        )

    @staticmethod
    def _runtime_input(payload: dict[str, Any]) -> list[dict[str, str]]:
        """Project the latest Responses user message into native multimodal input."""

        sources = payload.get("ResponsesInput") or payload.get("Messages") or []
        if not isinstance(sources, list):
            return []
        for message in reversed(sources):
            if not isinstance(message, dict) or str(message.get("role") or "user") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return []
            if not isinstance(content, list):
                continue
            items: list[dict[str, str]] = []
            has_attachment = False
            for part in content:
                if not isinstance(part, dict):
                    continue
                kind = str(part.get("type") or "")
                if kind in {"input_text", "text"}:
                    text = str(part.get("text") or "")
                    if text:
                        items.append({"type": "text", "text": text})
                elif kind in {"input_image", "image"}:
                    url = str(
                        part.get("image_url") or part.get("imageUrl") or part.get("url") or ""
                    )
                    if url:
                        items.append({"type": "image", "url": url})
                        has_attachment = True
                elif kind == "input_file":
                    data = str(part.get("file_data") or part.get("file_url") or "")
                    if data:
                        items.append(
                            {
                                "type": "input_file",
                                "file_data": data,
                                "filename": str(part.get("filename") or "attachment"),
                            }
                        )
                        has_attachment = True
            if items and has_attachment:
                return items
        return []

    @staticmethod
    def _active_status(status: RunStatus) -> str:
        return "running" if status in {RunStatus.RUNNING, RunStatus.WAITING_INPUT} else ""

    @staticmethod
    def _run_output(run: RunRecord) -> str:
        if run.output:
            return run.output
        if run.error:
            return str(run.error.get("message") or "Agent 运行失败")
        status = run.status.value if isinstance(run.status, RunStatus) else str(run.status)
        return f"运行状态：{status}"

    @staticmethod
    def _short_title(value: str, limit: int = 36) -> str:
        text = " ".join(value.split())
        return text if len(text) <= limit else f"{text[:limit]}..."

    @staticmethod
    def _timestamp(value: datetime | float | int | None) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, (float, int)):
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _sse(event: str, payload: dict[str, Any]) -> str:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return f"event: {event}\ndata: {data}\n\n"

    @classmethod
    def _failed_sse(cls, invocation_id: str, message: str) -> str:
        return cls._sse(
            "response.failed",
            {
                "type": "response.failed",
                "response": {
                    "id": invocation_id,
                    "status": "failed",
                    "error": {"message": message},
                },
                "error": {"message": message},
            },
        )


__all__ = ["StudioSharedWebBridge"]
