"""Client helpers for invoking remote A2A agents from KsADK code."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
from a2a.client import Client, ClientConfig, ClientFactory
from a2a.types import (
    Message,
    MessageSendConfiguration,
    Part,
    Role,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
    TextPart,
)
from a2a.utils import get_artifact_text, get_message_text

_FAILED_TASK_STATES = {
    TaskState.canceled,
    TaskState.failed,
    TaskState.rejected,
}


def _build_text_message(
    text: str,
    *,
    context_id: str | None = None,
    task_id: str | None = None,
) -> Message:
    return Message(
        role=Role.user,
        parts=[Part(root=TextPart(text=text))],
        message_id=str(uuid4()),
        context_id=context_id,
        task_id=task_id,
    )


def _task_output(task: Task) -> str:
    outputs = []
    for artifact in task.artifacts or []:
        artifact_text = get_artifact_text(artifact, delimiter="")
        if artifact_text:
            outputs.append(artifact_text)
    if outputs:
        return "".join(outputs)

    if task.status.message is not None:
        return get_message_text(task.status.message)

    if task.history:
        for message in reversed(task.history):
            if message.role == Role.agent:
                message_text = get_message_text(message)
                if message_text:
                    return message_text

    return ""


def _task_error(task: Task) -> str:
    text = _task_output(task)
    if text:
        return text
    return f"Remote A2A task ended in state: {task.status.state.value}"


class RemoteA2AClient:
    """Thin client wrapper for interacting with a remote A2A endpoint."""

    def __init__(
        self,
        endpoint: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/") or endpoint
        self.timeout = timeout
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._streaming_client: Client | None = None
        self._blocking_client: Client | None = None

    async def get_card(self) -> Any:
        client = await self._ensure_client(streaming=True)
        return await client.get_card()

    async def invoke(
        self,
        text: str,
        *,
        context_id: str | None = None,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = await self._ensure_client(streaming=False)
        message = _build_text_message(
            text,
            context_id=context_id,
            task_id=task_id,
        )
        final_task: Task | None = None

        async for event in client.send_message(
            message,
            configuration=MessageSendConfiguration(blocking=True),
            request_metadata=metadata,
        ):
            if isinstance(event, Message):
                return {
                    "output": get_message_text(event),
                    "task_id": event.task_id,
                    "context_id": event.context_id,
                    "message": event,
                }
            final_task, _ = event

        if final_task is None:
            raise RuntimeError("A2A response did not include a task result")

        self._raise_for_failed_task(final_task)
        return {
            "output": _task_output(final_task),
            "task_id": final_task.id,
            "context_id": final_task.context_id,
            "task": final_task,
        }

    async def stream(
        self,
        text: str,
        *,
        context_id: str | None = None,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        client = await self._ensure_client(streaming=True)
        message = _build_text_message(
            text,
            context_id=context_id,
            task_id=task_id,
        )
        emitted_output = False
        latest_task: Task | None = None

        async for event in client.send_message(
            message,
            configuration=MessageSendConfiguration(blocking=True),
            request_metadata=metadata,
        ):
            if isinstance(event, Message):
                text_output = get_message_text(event)
                if text_output:
                    emitted_output = True
                    yield {
                        "delta": text_output,
                        "type": "text",
                        "task_id": event.task_id,
                        "context_id": event.context_id,
                    }
                return

            latest_task, update = event
            if isinstance(update, TaskArtifactUpdateEvent):
                artifact_text = get_artifact_text(update.artifact)
                if artifact_text:
                    emitted_output = True
                    yield {
                        "delta": artifact_text,
                        "type": "text",
                        "task_id": update.task_id,
                        "context_id": update.context_id,
                    }
                continue

            if isinstance(update, TaskStatusUpdateEvent) and update.final:
                self._raise_for_failed_task(latest_task)

        if latest_task is None:
            return

        self._raise_for_failed_task(latest_task)
        if emitted_output:
            return

        task_text = _task_output(latest_task)
        if task_text:
            yield {
                "delta": task_text,
                "type": "text",
                "task_id": latest_task.id,
                "context_id": latest_task.context_id,
            }

    async def close(self) -> None:
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
        self._http_client = None
        self._streaming_client = None
        self._blocking_client = None

    async def __aenter__(self) -> "RemoteA2AClient":
        await self._ensure_client(streaming=True)
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def _ensure_client(self, *, streaming: bool) -> Client:
        cached_client = (
            self._streaming_client if streaming else self._blocking_client
        )
        if cached_client is not None:
            return cached_client

        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=self.timeout)

        client = await ClientFactory.connect(
            self.endpoint,
            client_config=ClientConfig(
                httpx_client=self._http_client,
                streaming=streaming,
                polling=False,
                accepted_output_modes=["text/plain"],
            ),
        )
        if streaming:
            self._streaming_client = client
        else:
            self._blocking_client = client
        return client

    @staticmethod
    def _raise_for_failed_task(task: Task) -> None:
        if task.status.state in _FAILED_TASK_STATES:
            raise RuntimeError(_task_error(task))


class RemoteA2AAgent:
    """Runner-like adapter for using a remote A2A endpoint inside KsADK flows."""

    def __init__(
        self,
        endpoint: str,
        *,
        name: str = "remote_a2a_agent",
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.name = name
        self._client = RemoteA2AClient(
            endpoint=endpoint,
            http_client=http_client,
            timeout=timeout,
        )

    async def get_card(self) -> Any:
        return await self._client.get_card()

    async def invoke(self, input_data: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.invoke(
            str(input_data.get("input", "")),
            context_id=input_data.get("context_id") or input_data.get("session_id"),
            task_id=input_data.get("task_id"),
            metadata=self._build_metadata(input_data),
        )
        return {
            "output": response["output"],
            "task_id": response.get("task_id"),
            "context_id": response.get("context_id"),
        }

    async def stream(
        self,
        input_data: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        async for event in self._client.stream(
            str(input_data.get("input", "")),
            context_id=input_data.get("context_id") or input_data.get("session_id"),
            task_id=input_data.get("task_id"),
            metadata=self._build_metadata(input_data),
        ):
            yield event

    async def close(self) -> None:
        await self._client.close()

    @staticmethod
    def _build_metadata(input_data: dict[str, Any]) -> dict[str, Any] | None:
        metadata = dict(input_data.get("metadata") or {})

        state = input_data.get("state")
        if isinstance(state, dict) and state:
            metadata["state"] = dict(state)

        branch = input_data.get("branch")
        if isinstance(branch, str) and branch:
            metadata["branch"] = branch

        return metadata or None
