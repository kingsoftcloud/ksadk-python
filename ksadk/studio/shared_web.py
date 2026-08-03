"""Adapter between AgentKit Studio state and the shared ``ksadk-web`` UI.

The Studio owns Agent authoring and immutable builds.  The shared Web package
owns the production conversation experience.  This module keeps that boundary
explicit by projecting Studio records onto the stable ``ksadk-web`` action
contract instead of maintaining a second chat implementation.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ksadk.studio.contracts import BuildStatus, OperationStatus, RunRecord, RunStatus
from ksadk.studio.errors import StudioError, not_found
from ksadk.studio.service import StudioService

_TERMINAL_OPERATIONS = {
    OperationStatus.SUCCEEDED,
    OperationStatus.FAILED,
    OperationStatus.CANCELLED,
    OperationStatus.INTERRUPTED,
}


def shared_web_static_root() -> Path | None:
    """Return the synchronized ``ksadk-web`` payload when it is available."""

    candidate = Path(__file__).resolve().parents[1] / "server" / "static"
    if (candidate / "index.html").is_file() and (candidate / "assets").is_dir():
        return candidate
    return None


class StudioSharedWebBridge:
    """Project Studio repositories onto the public ``ksadk-web`` API surface."""

    def __init__(self, studio: StudioService) -> None:
        self.studio = studio
        self._operations_by_invocation: dict[str, str] = {}

    def resolve_agent_id(self, requested: str | None = None) -> str:
        if requested:
            self.studio.drafts.get(requested)
            return requested
        agents = self.studio.drafts.list(limit=1)
        if not agents:
            raise not_found("agent", "")
        return agents[0].metadata.id

    def bootstrap(self, agent_id: str) -> dict[str, Any]:
        draft = self.studio.drafts.get(agent_id)
        model = self._model_descriptor(agent_id)
        return {
            "Agent": {
                "AgentId": agent_id,
                "Name": draft.metadata.name,
                "Framework": "agentkit",
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
                                "A2UI": False,
                                "Interrupt": False,
                                "Cancel": True,
                            },
                        }
                    ],
                },
                "RunLifecycle": {
                    "Enabled": True,
                    "Resume": False,
                    "Abort": True,
                    "Checkpoints": False,
                    "CheckpointResume": False,
                    "CheckpointResumePreview": False,
                },
                "ApprovalPolicy": {
                    "Modes": ["ask", "risk"],
                    "DefaultMode": "risk",
                    "RuntimeOverride": False,
                },
                "WorkspaceFiles": {"Enabled": False},
                "NativeDashboard": {"Enabled": False},
                "NativeTerminal": {"Enabled": False},
                "Thinking": False,
            },
        }

    def list_models(self, agent_id: str) -> dict[str, Any]:
        model = self._model_descriptor(agent_id)
        return {
            "Models": [model],
            "Current": model["id"],
            "Source": "agentkit-studio",
        }

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
        self.studio.drafts.get(agent_id)
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

    def list_messages(
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
                    "Activities": self._run_activities(run),
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

    def list_session_events(self, session_id: str) -> dict[str, Any]:
        runs = self.studio.event_store.list_runs(session_id=session_id)
        events: list[dict[str, Any]] = []
        sequence = 0
        for run in runs:
            for event in self.studio.event_store.events(run.id):
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

    async def stream_run(self, payload: dict[str, Any]) -> AsyncIterator[str]:
        agent_id = self.resolve_agent_id(str(payload.get("AgentId") or "") or None)
        session_id = str(payload.get("SessionId") or f"ses_{uuid4().hex}")
        invocation_id = str(payload.get("InvocationId") or f"run_{uuid4().hex}")
        prompt = self._input_text(payload)

        yield self._sse(
            "response.created",
            {
                "type": "response.created",
                "response": {"id": invocation_id, "status": "in_progress"},
            },
        )
        try:
            build = await self._ensure_build(agent_id)
            operation = self.studio.submit_run(
                build.id,
                prompt,
                session_id=session_id,
                idempotency_key=f"ksadk-web:{invocation_id}",
            )
            self._operations_by_invocation[invocation_id] = operation.id
            yield self._sse(
                "response.in_progress",
                {
                    "type": "response.in_progress",
                    "response": {"id": invocation_id, "status": "in_progress"},
                },
            )
            while True:
                current = self.studio.operations.get(operation.id)
                if current.status in _TERMINAL_OPERATIONS:
                    break
                yield ": keep-alive\n\n"
                await asyncio.sleep(0.2)

            if current.status != OperationStatus.SUCCEEDED:
                message = str((current.error or {}).get("message") or "Agent 运行失败")
                yield self._failed_sse(invocation_id, message)
                return

            run = self.studio.event_store.get(current.resource_id)
            if run.status != RunStatus.COMPLETED:
                yield self._failed_sse(invocation_id, self._run_output(run))
                return

            yield self._sse(
                "response.output_text.delta",
                {"type": "response.output_text.delta", "delta": run.output},
            )
            yield self._sse(
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "id": run.id,
                        "status": "completed",
                        "output": [],
                        "usage": {
                            "input_tokens": run.usage.input_tokens,
                            "output_tokens": run.usage.output_tokens,
                            "total_tokens": run.usage.total_tokens,
                        },
                    },
                },
            )
        except StudioError as exc:
            yield self._failed_sse(invocation_id, exc.message)
        except Exception:
            yield self._failed_sse(invocation_id, "本地 Agent 运行失败")
        finally:
            self._operations_by_invocation.pop(invocation_id, None)

    async def _ensure_build(self, agent_id: str):
        for record in self.studio.builds.list_for_agent(agent_id):
            if record.status == BuildStatus.SUCCEEDED:
                return record
        draft = self.studio.drafts.get(agent_id)
        if (
            draft.spec.model is None
            and not draft.spec.bindings.model_profile_id
        ):
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
            key=lambda run: run.started_at
            or run.completed_at
            or datetime.min.replace(tzinfo=timezone.utc),
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

    def _model_descriptor(self, agent_id: str) -> dict[str, Any]:
        draft = self.studio.drafts.get(agent_id)
        binding_id = draft.spec.bindings.model_profile_id
        spec = (
            self.studio.catalog.resolve_model(draft.spec.bindings)
            or draft.spec.model
        )
        if spec is None:
            model_id = "unconfigured-model"
            display_name = "未配置模型"
            max_output_tokens = 2048
        else:
            model_id = spec.model
            display_name = (
                self.studio.catalog.get(binding_id).display_name
                if binding_id
                else spec.model
            )
            max_output_tokens = spec.parameters.max_tokens
        return {
            "id": model_id,
            "display_name": display_name,
            "source": "agentkit-studio",
            "context_window_tokens": draft.spec.context.max_input_tokens,
            "max_output_tokens": max_output_tokens,
            "capabilities": {
                "function_calling": True,
                "structured_output": True,
            },
        }

    def _run_activities(self, run: RunRecord) -> list[dict[str, Any]]:
        activities: list[dict[str, Any]] = []
        for event in self.studio.event_store.events(run.id):
            operations = event.data.get("a2ui_operations") or event.data.get("operations")
            if not isinstance(operations, list):
                continue
            surface_id = str(
                event.data.get("surfaceId")
                or event.data.get("surface_id")
                or f"{run.id}-surface"
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
                if isinstance(part, dict)
                and str(part.get("type") or "") in {"input_text", "text"}
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


__all__ = ["StudioSharedWebBridge", "shared_web_static_root"]
