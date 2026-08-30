"""Project AgentKit Studio runs onto Responses and compatibility API contracts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ksadk.studio.contracts import BuildStatus, OperationStatus, RunRecord, RunStatus
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
                "WorkspaceFiles": {"Enabled": False},
                "NativeDashboard": {"Enabled": False},
                "NativeTerminal": {"Enabled": False},
                "Thinking": False,
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

    def list_sessions(
        self,
        agent_id: str,
        *,
        page: int = 1,
        page_size: int = 30,
    ) -> dict[str, Any]:
        sessions = self._sessions(agent_id)
        safe_page = max(1, page)
        safe_size = min(100, max(1, page_size))
        start = (safe_page - 1) * safe_size
        return {
            "Sessions": sessions[start : start + safe_size],
            "Total": len(sessions),
            "Page": safe_page,
            "PageSize": safe_size,
        }

    def create_session(self, agent_id: str) -> dict[str, Any]:
        self._draft(agent_id)
        session_id = f"ses_{uuid4().hex}"
        now = datetime.now(timezone.utc).isoformat()
        return {
            "Session": {
                "SessionId": session_id,
                "AgentId": agent_id,
                "UserId": "local-user",
                "Title": "新会话",
                "CreatedAt": now,
                "UpdatedAt": now,
            }
        }

    def get_session(self, session_id: str) -> dict[str, Any]:
        runs = self.studio.event_store.list_runs(session_id=session_id)
        if not runs:
            raise not_found("session", session_id)
        return {"Session": self._session_record(runs)}

    def delete_session(self, session_id: str) -> dict[str, Any]:
        self.studio.event_store.delete_session(session_id)
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
                    "Content": {"text": self._run_output(run)},
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
                        "EventType": event.type,
                        "InvocationId": run.id,
                        "Content": event.data,
                        "Timestamp": self._timestamp(event.created_at),
                    }
                )
        return {
            "Events": events,
            "Total": len(events),
            "Offset": 0,
            "Limit": len(events),
        }

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

    async def stream_run(self, payload: dict[str, Any]) -> AsyncIterator[str]:
        agent_id = self.resolve_agent_id(str(payload.get("AgentId") or "") or None)
        session_id = str(payload.get("SessionId") or f"ses_{uuid4().hex}")
        invocation_id = str(payload.get("InvocationId") or f"resp_{uuid4().hex}")
        prompt = self._input_text(payload)
        runtime_input = self._runtime_input(payload)
        model = self._select_model(agent_id, str(payload.get("Model") or ""))
        approval_mode = str(payload.get("ApprovalMode") or "")
        collaboration_mode = str(payload.get("CollaborationMode") or "")
        goal_objective = str(payload.get("GoalObjective") or "")
        reasoning_effort = str(payload.get("ReasoningEffort") or "")
        execution = asyncio.create_task(
            self._execute_run(
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
        approval_mode = str(payload.get("ApprovalMode") or "")
        collaboration_mode = str(payload.get("CollaborationMode") or "")
        goal_objective = str(payload.get("GoalObjective") or "")
        reasoning_effort = str(payload.get("ReasoningEffort") or "")
        try:
            run = await self._execute_run(
                agent_id=agent_id,
                session_id=session_id,
                invocation_id=invocation_id,
                prompt=self._input_text(payload),
                runtime_input=self._runtime_input(payload),
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
        build = await self._ensure_build(agent_id)

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
                    else "completed" if done else "in_progress"
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
                    else "completed" if done else "in_progress"
                ),
                "output": data.get("output") or data.get("result") or "",
            }
        elif event_type == "approval.requested":
            item = {
                "id": call_id or f"approval_{uuid4().hex}",
                "call_id": call_id,
                "type": "approval_request",
                "status": "in_progress",
                "action": data,
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
        if self.studio.is_codex_agent(agent_id):
            self.studio.codex_agent_detail(agent_id)
            builds = [
                record
                for record in self.studio.codex_builds.list()
                if record.agent_name == agent_id and self.studio.codex_builder.is_current(record)
            ]
            if builds:
                return builds[0]
            return await asyncio.to_thread(self.studio.codex_builder.build, agent_id)
        for record in self.studio.builds.list_for_agent(agent_id):
            if record.status == BuildStatus.SUCCEEDED:
                return record
        draft = self.studio.drafts.get(agent_id)
        if draft.spec.model is None and not draft.spec.bindings.model_profile_id:
            raise StudioError(
                "AGENT_MODEL_REQUIRED",
                "当前 Agent 未绑定 Model Profile，请先在 Agent 配置中选择模型；"
                "API Key 只提供访问凭证，不会自动绑定模型。",
                status_code=422,
                field="spec.bindings.modelProfileId",
            )
        return await asyncio.to_thread(self.studio.builder.build, draft)

    def _sessions(self, agent_id: str) -> list[dict[str, Any]]:
        grouped: dict[str, list[RunRecord]] = {}
        for run in self.studio.event_store.list_runs():
            if run.agent_id != agent_id:
                continue
            grouped.setdefault(run.session_id, []).append(run)
        records = [self._session_record(runs) for runs in grouped.values()]
        records.sort(key=lambda item: item["UpdatedAt"], reverse=True)
        return records

    def _session_record(self, runs: list[RunRecord]) -> dict[str, Any]:
        ordered = sorted(
            runs,
            key=lambda run: (
                run.started_at or run.completed_at or datetime.min.replace(tzinfo=timezone.utc)
            ),
        )
        first = ordered[0]
        latest = ordered[-1]
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
            "ActiveRunStatus": self._active_status(latest.status),
            "ActiveInvocationId": latest.id if latest.status == RunStatus.RUNNING else "",
            "TokenUsage": usage,
        }

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
            "context_window_tokens": draft.spec.context.max_input_tokens,
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
            "context_window_tokens": metadata.get("context_window_tokens")
            or draft.spec.context.max_input_tokens,
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
            "context_window_tokens": draft.spec.context.max_input_tokens,
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
        activities: list[dict[str, Any]] = []
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
            activities.append(
                {
                    "SeqId": event.id,
                    "Type": event.type,
                    "MessageId": f"{run.id}:assistant",
                    "SurfaceId": surface_id,
                    "Content": {"a2ui_operations": operations},
                }
            )
        return activities

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
            has_image = False
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
                        has_image = True
            if items and has_image:
                return items
        return []

    @staticmethod
    def _active_status(status: RunStatus) -> str:
        return "running" if status == RunStatus.RUNNING else ""

    @staticmethod
    def _run_output(run: RunRecord) -> str:
        if run.output:
            return run.output
        if run.error:
            return str(run.error.get("message") or "Agent 运行失败")
        return f"运行状态：{run.status.value}"

    @staticmethod
    def _short_title(value: str, limit: int = 36) -> str:
        text = " ".join(value.split())
        return text if len(text) <= limit else f"{text[:limit]}..."

    @staticmethod
    def _timestamp(value: datetime | None) -> str:
        return (value or datetime.now(timezone.utc)).isoformat()

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
