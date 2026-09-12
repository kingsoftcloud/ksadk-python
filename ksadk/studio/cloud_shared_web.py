"""Project cloud-chat (CloudDeploymentService) onto the shared
/agentengine/api/v1 action contract, so the ksadk-web headless data layer
(useAgentChat / AgentWorkbench) drives cloud agents exactly like hosted-ui.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from ksadk.studio.cloud import CloudDeploymentService
from ksadk.studio.errors import StudioError, not_found
from ksadk.tools.gateway import tool_approval_capability


def is_cloud_agent_id(agent_id: str) -> bool:
    normalized = agent_id.strip()
    return normalized.startswith("account:ar-") or normalized.startswith("ar-")


def cloud_chat_target(agent_id: str) -> str:
    normalized = agent_id.strip()
    if normalized.startswith("account:"):
        return normalized
    return f"account:{normalized}"


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_text(item) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "content", "value"):
            if key in value:
                return _text(value[key])
    return ""


def _input_text(payload: dict[str, Any]) -> str:
    # Mirror StudioSharedWebBridge._input_text: the RunAgent payload carries the
    # turn as a Responses-style message list.
    sources = payload.get("ResponsesInput") or payload.get("Messages") or []
    if not isinstance(sources, list):
        return ""
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
    return ""


def _timestamp(value: Any) -> str:
    raw = str(value or "")
    return raw or datetime.now(timezone.utc).isoformat()


class CloudSharedWebBridge:
    """Shared-web action semantics backed by the cloud-chat proxy."""

    def __init__(self, cloud: CloudDeploymentService | Any) -> None:
        # The desktop can keep several workspace runtimes alive while the
        # active workspace changes.  Resolve the cloud service per request so
        # an awaited call cannot continue against the previous workspace.
        self._cloud_or_manager = cloud

    @property
    def cloud(self) -> CloudDeploymentService:
        active = getattr(self._cloud_or_manager, "active", None)
        return getattr(active, "cloud", self._cloud_or_manager)

    @staticmethod
    def require_session_id(payload: dict[str, Any]) -> str:
        session_id = str(payload.get("SessionId") or "").strip()
        if not session_id:
            raise StudioError("SESSION_ID_REQUIRED", "云端运行需要会话标识", status_code=400)
        return session_id

    async def bootstrap(self, agent_id: str) -> dict[str, Any]:
        target = cloud_chat_target(agent_id)
        normalized_agent_id = target.removeprefix("account:")
        detail = await self.cloud.get_account_agent(normalized_agent_id)
        return {
            "Agent": {
                "AgentId": normalized_agent_id,
                "Name": str(detail.get("name") or normalized_agent_id),
                "Framework": str(detail.get("framework") or detail.get("runtimeType") or "cloud"),
            },
            "AccessMode": "Owner",
            "ApiFormats": ["responses"],
            "Capabilities": {
                # Cloud AgentKernel exposes durable interactions through the
                # account-scoped SubmitInteraction action.  Advertising this
                # keeps approvals on that protocol instead of falling back to
                # the legacy empty-message RunAgent resume path.
                "interaction_v1": {"enabled": True},
                "HostedChat": {
                    "Enabled": True,
                    "ApiFormats": ["responses"],
                    "PreferredTransport": "responses",
                    "Transports": [
                        {
                            "Protocol": "responses",
                            "Runtime": "agentengine-cloud",
                            "Endpoint": "/agentengine/api/v1/RunAgent",
                            "Version": "v1",
                            "Capabilities": {
                                "A2UI": True,
                                "Interrupt": True,
                                "Cancel": False,
                            },
                        }
                    ],
                },
                "RunLifecycle": {
                    "Enabled": True,
                    "Resume": False,
                    "Abort": False,
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

    async def list_models(self, agent_id: str) -> dict[str, Any]:
        payload = await self.cloud.list_cloud_chat_models(cloud_chat_target(agent_id))
        rows = payload.get("models") or payload.get("items") or []
        current = str(payload.get("current") or payload.get("configured_model") or "")
        models: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, str):
                models.append({"id": row, "display_name": row})
                continue
            if not isinstance(row, dict):
                continue
            model_id = str(row.get("id") or row.get("model") or row.get("name") or "").strip()
            if not model_id:
                continue
            models.append(
                {
                    "id": model_id,
                    "display_name": str(
                        row.get("display_name")
                        or row.get("displayName")
                        or row.get("label")
                        or model_id
                    ),
                    "capabilities": (
                        row.get("capabilities")
                        if isinstance(row.get("capabilities"), dict)
                        else None
                    ),
                }
            )
        if current and not any(model["id"] == current for model in models):
            current = models[0]["id"] if models else ""
        return {"Models": models, "Current": current, "Source": "agentengine-cloud"}

    async def list_sessions(
        self,
        agent_id: str,
        *,
        page: int = 1,
        page_size: int = 30,
    ) -> dict[str, Any]:
        payload = await self.cloud.list_cloud_chat_sessions(
            cloud_chat_target(agent_id), page=page, size=page_size
        )
        rows = payload.get("sessions") or payload.get("items") or []
        sessions: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            session_id = str(
                row.get("session_id") or row.get("sessionId") or row.get("id") or ""
            ).strip()
            if not session_id:
                continue
            sessions.append(
                {
                    "SessionId": session_id,
                    "AgentId": agent_id,
                    "Title": _text(
                        row.get("title")
                        or row.get("summary")
                        or row.get("first_prompt")
                        or row.get("last_prompt")
                    )
                    or "新会话",
                    "UpdatedAt": _timestamp(
                        row.get("updated_at") or row.get("createdAt") or row.get("created_at")
                    ),
                    "ActiveRunStatus": str(row.get("active_run_status") or row.get("state") or ""),
                    "ActiveRunError": _text(
                        row.get("active_run_error") or row.get("last_error") or ""
                    ),
                }
            )
        return {
            "Sessions": sessions,
            "Total": int(payload.get("total") or len(sessions)),
            "Page": max(1, page),
            "PageSize": min(100, max(1, page_size)),
        }

    async def create_session(self, agent_id: str) -> dict[str, Any]:
        payload = await self.cloud.create_cloud_chat_session(cloud_chat_target(agent_id))
        raw = payload.get("session") or payload.get("Session") or payload
        if not isinstance(raw, dict):
            raise StudioError("CLOUD_SESSION_CREATE_FAILED", "云端未返回有效会话", status_code=502)
        session_id = str(
            raw.get("session_id") or raw.get("sessionId") or raw.get("id") or ""
        ).strip()
        if not session_id:
            raise StudioError(
                "CLOUD_SESSION_CREATE_FAILED", "云端未返回有效会话标识", status_code=502
            )
        now = datetime.now(timezone.utc).isoformat()
        return {
            "Session": {
                "SessionId": session_id,
                "AgentId": agent_id,
                "Title": _text(raw.get("title") or raw.get("first_prompt")) or "新会话",
                "CreatedAt": _timestamp(raw.get("created_at") or now),
                "UpdatedAt": _timestamp(raw.get("updated_at") or now),
            }
        }

    async def get_session(self, agent_id: str, session_id: str) -> dict[str, Any]:
        payload = await self.list_sessions(agent_id)
        for row in payload["Sessions"]:
            if row["SessionId"] == session_id:
                return {"Session": row}
        raise not_found("session", session_id)

    async def delete_session(self, agent_id: str, session_id: str) -> dict[str, Any]:
        await self.cloud.delete_cloud_chat_session(
            cloud_chat_target(agent_id), session_id=session_id
        )
        return {}

    async def list_messages(
        self,
        agent_id: str,
        session_id: str,
        *,
        after_seq_id: int | None = None,
        before_seq_id: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        payload = await self.cloud.list_cloud_chat_messages(
            cloud_chat_target(agent_id),
            session_id=session_id,
            after_seq_id=after_seq_id,
            before_seq_id=before_seq_id,
            limit=min(200, max(1, limit)),
        )
        rows = payload.get("messages") or []
        messages: list[dict[str, Any]] = []
        latest_seq_id = int(payload.get("latest_seq_id") or payload.get("latestSeqId") or 0)
        for row in rows:
            if not isinstance(row, dict):
                continue
            seq_id = int(row.get("seq_id") or row.get("SeqId") or 0)
            latest_seq_id = max(latest_seq_id, seq_id)
            content = row.get("content")
            messages.append(
                {
                    "MessageId": str(
                        row.get("message_id")
                        or row.get("messageId")
                        or row.get("id")
                        or f"msg-{seq_id}"
                    ),
                    "Role": str(row.get("role") or "assistant"),
                    "Content": {"text": _text(content)},
                    "Timestamp": _timestamp(row.get("timestamp")),
                    "SeqId": seq_id,
                    "InvocationId": str(row.get("invocation_id") or row.get("run_id") or ""),
                }
            )
        selected = messages
        if after_seq_id is not None:
            selected = [item for item in selected if item["SeqId"] > after_seq_id]
        if before_seq_id is not None:
            selected = [item for item in selected if item["SeqId"] < before_seq_id]
        safe_limit = min(200, max(1, limit))
        upstream_has_more = payload.get("has_more", payload.get("HasMore"))
        has_more = (
            bool(upstream_has_more) if upstream_has_more is not None else len(selected) > safe_limit
        )
        window = selected[-safe_limit:]
        upstream_cursor = payload.get("next_cursor", payload.get("NextCursor"))
        next_cursor = upstream_cursor
        if next_cursor is None and has_more and window:
            next_cursor = window[0]["SeqId"]
        return {
            "Messages": window,
            "LatestSeqId": latest_seq_id,
            "HasMore": has_more,
            "NextCursor": next_cursor,
        }

    async def list_session_events(
        self,
        agent_id: str,
        session_id: str,
        *,
        after_seq_id: int | None = None,
        offset: int | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        safe_limit = min(500, max(1, limit))
        payload = await self.cloud.list_cloud_chat_events(
            cloud_chat_target(agent_id),
            session_id=session_id,
            after_seq_id=after_seq_id,
            offset=offset,
            limit=safe_limit,
        )
        rows = payload.get("events") or []
        events: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            events.append(
                {
                    "SeqId": int(row.get("SeqId") or row.get("seq_id") or row.get("seqId") or 0),
                    "EventId": str(
                        row.get("EventId") or row.get("event_id") or row.get("eventId") or ""
                    ),
                    "EventType": str(
                        row.get("EventType") or row.get("event_type") or row.get("eventType") or ""
                    ),
                    "InvocationId": str(
                        row.get("InvocationId")
                        or row.get("invocation_id")
                        or row.get("run_id")
                        or ""
                    ),
                    "Content": row.get("Content") or row.get("content") or {},
                    "Timestamp": _timestamp(row.get("Timestamp") or row.get("timestamp")),
                }
            )
        return {
            "Events": events,
            "Total": int(payload.get("total") or len(events)),
            "Offset": int(
                payload.get("offset") if payload.get("offset") is not None else (offset or 0)
            ),
            "Limit": int(payload.get("limit") if payload.get("limit") is not None else safe_limit),
        }

    async def subscribe_run_events(
        self,
        agent_id: str,
        session_id: str,
        invocation_id: str,
        *,
        after_seq_id: int = 0,
    ) -> AsyncIterator[str]:
        """Project cloud event polling onto the shared-Web SSE contract.

        The account API exposes durable ``ListSessionEvents`` reads rather
        than a browser-facing SSE URL.  After an interaction receipt is
        accepted, keep the loopback connection open and forward new events
        for the same invocation so the UI can resume without a page reload.
        """

        cursor = max(0, after_seq_id)
        deadline = time.monotonic() + 5 * 60
        terminal_types = {
            "run.completed",
            "run.failed",
            "run.cancelled",
            "run.interrupted",
            "response.completed",
            "response.failed",
            "response.cancelled",
        }
        while time.monotonic() < deadline:
            history = await self.list_session_events(
                agent_id,
                session_id,
                after_seq_id=cursor,
                limit=500,
            )
            terminal = False
            for event in history["Events"]:
                cursor = max(cursor, int(event.get("SeqId") or 0))
                if str(event.get("InvocationId") or "") != invocation_id:
                    continue
                yield "event: message\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                if str(event.get("EventType") or "").lower() in terminal_types:
                    terminal = True
            if terminal:
                yield "event: done\ndata: [DONE]\n\n"
                return
            yield ": ping\n\n"
            await asyncio.sleep(0.5)
        yield "event: done\ndata: [DONE]\n\n"

    async def open_run_stream(
        self, agent_id: str, payload: dict[str, Any]
    ) -> AsyncIterator[bytes]:
        """Open the upstream response before Studio commits its SSE headers."""

        session_id = self.require_session_id(payload)
        prompt = _input_text(payload)
        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}] if prompt else []
        model = str(payload.get("Model") or "").strip() or None
        approval_mode = str(payload.get("ApprovalMode") or "").strip() or None
        collaboration_mode = str(payload.get("CollaborationMode") or "").strip() or None
        goal_objective = (
            str(payload.get("GoalObjective") or payload.get("goal_objective") or "").strip() or None
        )
        return await self.cloud.stream_cloud_chat_message(
            cloud_chat_target(agent_id),
            session_id=session_id,
            content=content,
            model=model,
            tool_approval_mode=approval_mode,
            collaboration_mode=collaboration_mode,
            goal_objective=goal_objective,
        )

    async def stream_run(
        self,
        agent_id: str,
        payload: dict[str, Any],
        *,
        upstream_stream: AsyncIterator[bytes] | None = None,
    ) -> AsyncIterator[str]:
        stream = upstream_stream
        if stream is None:
            stream = await self.open_run_stream(agent_id, payload)
        # Older cloud runtimes end with a bare Responses object. Newer ones
        # already emit canonical terminal events. Inspect without rewriting
        # the live deltas, and synthesize only the missing terminal state.
        frame_buffer = ""
        last_response: dict[str, Any] | None = None
        terminal_event = ""
        saw_terminal_event = False
        saw_done = False
        saw_pending_interaction = False

        def inspect_frame(frame: str) -> None:
            nonlocal last_response, terminal_event, saw_terminal_event
            nonlocal saw_done, saw_pending_interaction
            event_name = ""
            data_lines: list[str] = []
            for line in frame.splitlines():
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            raw = "\n".join(data_lines).strip()
            if raw == "[DONE]" or event_name == "done":
                saw_done = True
            observed_types = {event_name} if event_name else set()
            if raw and raw != "[DONE]":
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    data_type = str(parsed.get("type") or "").strip()
                    if data_type:
                        observed_types.add(data_type)
                    response = parsed.get("response")
                    if isinstance(response, dict):
                        last_response = response
                    elif str(parsed.get("object") or "").lower() == "response":
                        last_response = parsed
            for observed in observed_types:
                if observed in {
                    "response.completed",
                    "response.failed",
                    "response.incomplete",
                    "response.cancelled",
                    "response.canceled",
                }:
                    terminal_event = observed
                    saw_terminal_event = True
                if observed in {
                    "interaction.requested",
                    "approval_request",
                    "response.approval_request",
                }:
                    saw_pending_interaction = True

        async for chunk in stream:
            text = (
                chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
            )
            frame_buffer += text
            while match := re.search(r"\r?\n\r?\n", frame_buffer):
                inspect_frame(frame_buffer[: match.start()])
                frame_buffer = frame_buffer[match.end() :]
            yield text
        if frame_buffer.strip():
            inspect_frame(frame_buffer)

        status = str((last_response or {}).get("status") or "").lower()
        if not terminal_event and status:
            terminal_event = {
                "completed": "response.completed",
                "failed": "response.failed",
                "incomplete": "response.incomplete",
                "cancelled": "response.cancelled",
                "canceled": "response.cancelled",
            }.get(status, "")
        if not terminal_event and not saw_pending_interaction:
            terminal_event = "response.incomplete"
            last_response = last_response or {
                "object": "response",
                "status": "incomplete",
                "incomplete_details": {"reason": "upstream_stream_ended_without_terminal"},
            }
        if terminal_event and not saw_terminal_event:
            terminal_event = (
                "response.cancelled" if terminal_event == "response.canceled" else terminal_event
            )
            terminal_payload = {
                "type": terminal_event,
                "response": last_response
                or {"object": "response", "status": terminal_event.removeprefix("response.")},
            }
            terminal_json = json.dumps(terminal_payload, ensure_ascii=False)
            yield f"event: {terminal_event}\ndata: {terminal_json}\n\n"
        if not saw_done:
            yield "event: done\ndata: [DONE]\n\n"

    async def submit_interaction(
        self,
        agent_id: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self.cloud.submit_cloud_chat_interaction(
            cloud_chat_target(agent_id),
            session_id=session_id,
            run_id=str(payload.get("RunId") or ""),
            interaction_id=str(payload.get("InteractionId") or ""),
            expected_revision=int(payload.get("ExpectedRevision") or 1),
            action=str(payload.get("Action") or "approve"),
            response=payload.get("Response") if isinstance(payload.get("Response"), dict) else {},
            idempotency_key=str(payload.get("IdempotencyKey") or ""),
        )
