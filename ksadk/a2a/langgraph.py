"""LangGraph 编排方调远端 A2A agent 的流式 helper。

ADK 有原生 ``RemoteA2aAgent``;LangGraph 没有等价封装。这里提供结构化流式
helper,保留远端 ``thinking`` / ``text`` 类型,并可直接写入 LangGraph custom
stream,无需 task store、无需 Space 绑定——直接走 a2a-sdk 原生 client。
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import AsyncIterator
from typing import Any, Callable, Literal, TypedDict

import httpx
from a2a.client import ClientCallContext, ClientConfig, create_client
from a2a.client.card_resolver import A2ACardResolver
from a2a.types import Message, Part, Role, SendMessageRequest


class A2AStreamEvent(TypedDict):
    """可直接交给 LangGraph custom writer 的远端流式事件。"""

    type: Literal["thinking", "text"]
    delta: str
    replace: bool


def _base_url(agent_card_url: str) -> str:
    """从 agent_card_url 取 base_url(剥掉 well-known 路径后缀)。"""
    url = agent_card_url.strip().rstrip("/")
    for suffix in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


async def stream_a2a_agent_events(
    agent_card_url: str,
    message: str,
    *,
    httpx_client: httpx.AsyncClient | None = None,
    timeout: float = 60.0,
) -> AsyncIterator[A2AStreamEvent]:
    """流式调用远端 A2A agent,保留 reasoning 与正文的结构化类型。

    在 langgraph node 里配合 custom stream 透传::

        from langgraph.config import get_stream_writer
        from ksadk.a2a.langgraph import stream_a2a_agent_events

        async def call_demo(state, config):
            writer = get_stream_writer()
            collected = ""
            async for event in stream_a2a_agent_events(
                "http://127.0.0.1:8094", state["query"]
            ):
                writer(event)
                if event["type"] == "text":
                    collected += event["delta"]
            return {"result": collected}

    Args:
        agent_card_url: 远端 agent 的 base URL(如 ``http://127.0.0.1:8094``)
            或完整 agent-card.json URL。
        message: 发给远端 agent 的用户消息。
        httpx_client: 可选复用的 ``httpx.AsyncClient``;不传则内部创建。
        timeout: httpx 请求超时秒数。

    Yields:
        ``{"type": "thinking"|"text", "delta": str, "replace": bool}``。
        reasoning 由 ``adk_thought`` Part metadata 识别,status_update 不伪装
        成思考；``replace`` 标识权威快照替换既有同类输出。
    """
    own_client = httpx_client is None
    client_httpx = httpx_client or httpx.AsyncClient(timeout=timeout)
    try:
        resolver = A2ACardResolver(httpx_client=client_httpx, base_url=_base_url(agent_card_url))
        card = await resolver.get_agent_card()
        client = await create_client(
            agent=card,
            client_config=ClientConfig(httpx_client=client_httpx, streaming=True),
        )
        request = SendMessageRequest(
            message=Message(
                role=Role.ROLE_USER,
                parts=[Part(text=message)],
                message_id=f"lg-a2a-{uuid.uuid4().hex}",
            )
        )
        snapshots: dict[tuple[str, str], str] = {}
        async for event in client.send_message(request, context=ClientCallContext()):
            for stream_event in _extract_artifact_events(event, snapshots):
                yield stream_event
    finally:
        if own_client:
            await client_httpx.aclose()


async def stream_a2a_agent(
    agent_card_url: str,
    message: str,
    *,
    httpx_client: httpx.AsyncClient | None = None,
    timeout: float = 60.0,
) -> AsyncIterator[str]:
    """兼容旧接口:只产出正文文本增量,reasoning 不混入最终答案。

    该字符串流无法表达 ``replace`` 语义。需要正确处理远端权威快照的调用方
    应使用 ``stream_a2a_agent_events`` 或 ``stream_a2a_agent_to_writer``。
    """
    async for event in stream_a2a_agent_events(
        agent_card_url,
        message,
        httpx_client=httpx_client,
        timeout=timeout,
    ):
        if event["type"] == "text":
            yield event["delta"]


async def stream_a2a_agent_to_writer(
    agent_card_url: str,
    message: str,
    *,
    writer: Callable[[A2AStreamEvent], Any] | None = None,
    httpx_client: httpx.AsyncClient | None = None,
    timeout: float = 60.0,
) -> str:
    """将远端 typed events 写入 LangGraph custom stream,返回纯正文结果。

    ``writer`` 不传时自动使用当前 LangGraph node 的 ``get_stream_writer()``。
    demo 只需调用本函数,无需了解 A2A wire artifact、append 或快照去重。
    """
    if writer is None:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()

    output: list[str] = []
    async for event in stream_a2a_agent_events(
        agent_card_url,
        message,
        httpx_client=httpx_client,
        timeout=timeout,
    ):
        write_result = writer(event)
        if inspect.isawaitable(write_result):
            await write_result
        if event["type"] == "text":
            if event["replace"]:
                output = [event["delta"]]
            else:
                output.append(event["delta"])
    return "".join(output)


def _extract_artifact_events(
    event: Any,
    snapshots: dict[tuple[str, str], str],
) -> list[A2AStreamEvent]:
    """从 A2A artifact_update 提取去重后的 typed events。

    快照按 ``(artifact_id, type)`` 隔离,避免 reasoning 与 response 互相覆盖。
    ``append=False`` 按累计快照 diff,``append=True`` 按增量直接透传。
    """
    which = None
    try:
        which = event.WhichOneof("payload")
    except Exception:  # 非 protobuf 或结构不符时静默跳过
        which = None
    if which == "artifact_update":
        artifact_update = getattr(event, "artifact_update", None)
        artifact = getattr(artifact_update, "artifact", None)
        if artifact is None:
            return []
        return _events_from_artifact(
            artifact,
            append=bool(getattr(artifact_update, "append", False)),
            snapshots=snapshots,
        )
    if which == "task":
        result: list[A2AStreamEvent] = []
        task = getattr(event, "task", None)
        for artifact in getattr(task, "artifacts", []):
            result.extend(_events_from_artifact(artifact, append=False, snapshots=snapshots))
        return result
    if which == "message":
        message = getattr(event, "message", None)
        if message is None:
            return []
        artifact = _MessageArtifact(message)
        return _events_from_artifact(artifact, append=False, snapshots=snapshots)
    return []


class _MessageArtifact:
    """把直接 Message 响应投影成 artifact-like 对象复用解析逻辑。"""

    def __init__(self, message: Any) -> None:
        self.artifact_id = f"message:{getattr(message, 'message_id', '') or 'anonymous'}"
        self.name = "response"
        self.parts = getattr(message, "parts", [])
        self.metadata = getattr(message, "metadata", None)


def _events_from_artifact(
    artifact: Any,
    *,
    append: bool,
    snapshots: dict[tuple[str, str], str],
) -> list[A2AStreamEvent]:
    """解析一个 artifact-like 对象，支持增量、累计快照和权威替换。"""

    artifact_id = str(
        getattr(artifact, "artifact_id", "")
        or getattr(artifact, "name", "")
        or "anonymous"
    )
    artifact_is_reasoning = (
        str(getattr(artifact, "name", "")).lower() == "reasoning"
        or _metadata_flag(getattr(artifact, "metadata", None), "adk_thought")
    )
    grouped: list[tuple[str, str, bool]] = []
    for part in getattr(artifact, "parts", []):
        text = str(getattr(part, "text", "") or "")
        if not text:
            continue
        event_type = (
            "thinking"
            if artifact_is_reasoning
            or _metadata_flag(getattr(part, "metadata", None), "adk_thought")
            else "text"
        )
        replaces_output = _metadata_flag(
            getattr(part, "metadata", None),
            "ksadk_output_snapshot",
        )
        if grouped and grouped[-1][0] == event_type:
            grouped[-1] = (
                event_type,
                grouped[-1][1] + text,
                grouped[-1][2] or replaces_output,
            )
        else:
            grouped.append((event_type, text, replaces_output))

    result: list[A2AStreamEvent] = []
    for event_type, text, replaces_output in grouped:
        key = (artifact_id, event_type)
        snapshot = snapshots.get(key, "")
        if append:
            delta = text
            snapshots[key] = snapshot + text
            replace = False
        else:
            delta = text[len(snapshot) :] if text.startswith(snapshot) else text
            snapshots[key] = text
            replace = replaces_output or bool(snapshot and not text.startswith(snapshot))
        if delta:
            result.append(
                {
                    "type": event_type,  # type: ignore[typeddict-item]
                    "delta": delta,
                    "replace": replace,
                }
            )
    return result


def _metadata_flag(metadata: Any, key: str) -> bool:
    if metadata is None:
        return False
    try:
        return bool(metadata[key])
    except (KeyError, TypeError, ValueError):
        return False


__all__ = [
    "A2AStreamEvent",
    "stream_a2a_agent",
    "stream_a2a_agent_events",
    "stream_a2a_agent_to_writer",
]
