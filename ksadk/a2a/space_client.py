"""A2ASpaceClient — Space 内动态发现与调用 (goal-06 §3.2)。

契约:Runtime 经环境变量 ``AGENTENGINE_A2A_SPACE_ID`` 获得绑定的 Space ID(§4.5,
locator 非授权),用 internal authenticated KOP facade ``ListA2ASpaceAgents``(§5.5)
动态发现该 Space 中 hosted/external Agent 的 latest AgentCard,再按
``supportedInterfaces`` 调用。external 调用受 Runtime 公网出站能力约束(egress,
§5.4:``A2A_SPACE_REQUIRES_PUBLIC_EGRESS``)。

面向 Agent 开发者的最小 interface(§3.2)::

    client = A2ASpaceClient.from_env()
    agents = await client.discover(prompt="查询天气")
    task = await client.send_message(agent_id=agents[0].id, message=message)
    async for event in client.subscribe(task.id): ...
    await client.cancel(task.id)
"""

from __future__ import annotations

import logging
import os
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from a2a.client import ClientCallContext, ClientConfig, create_client
from a2a.types import (
    AgentCard,
    CancelTaskRequest,
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    SubscribeToTaskRequest,
    TaskState,
)
from google.protobuf.json_format import MessageToDict

from ksadk.a2a.credential import A2ACredentialProvider
from ksadk.a2a.event_adapter import A2AEventAdapter
from ksadk.common.aicp_env import resolve_aicp_connection
from ksadk.events.runtime_event import RuntimeEvent

logger = logging.getLogger(__name__)

#: 环境变量(§4.5 / §5.4)。统一使用 KSADK_ 前缀,与 Skill 空间约定一致。
ENV_A2A_SPACE_ID = "KSADK_A2A_SPACE_ID"
#: 显式指定 discovery 服务地址;缺省走 resolve_aicp_connection 自动探测(同 Skill)。
ENV_A2A_SERVICE_URL = "KSADK_A2A_SERVICE_URL"
ENV_A2A_ENABLE_PUBLIC_EGRESS = "KSADK_A2A_ENABLE_PUBLIC_EGRESS"
#: 旧变量名,仅作兼容兜底;新部署不应再依赖。
_LEGACY_ENV_A2A_SPACE_ID = "AGENTENGINE_A2A_SPACE_ID"
_LEGACY_ENV_SERVER_URL = "AGENTENGINE_SERVER_URL"
_LEGACY_ENV_A2A_ENABLE_PUBLIC_EGRESS = "AGENTENGINE_A2A_ENABLE_PUBLIC_EGRESS"


def _resolve_a2a_service_url() -> str:
    """解析 discovery endpoint:显式 URL > 旧 AGENTENGINE_SERVER_URL > AICP 自动探测。"""
    explicit = os.getenv(ENV_A2A_SERVICE_URL, "").strip()
    if explicit:
        return explicit.rstrip("/")
    legacy = os.getenv(_LEGACY_ENV_SERVER_URL, "").strip()
    if legacy:
        return legacy.rstrip("/")
    connection = resolve_aicp_connection("KSADK_A2A")
    return f"{connection['scheme']}://{connection['endpoint']}".rstrip("/")


#: egress 关闭时调 external 的错误码(§5.4)。
ERR_REQUIRES_PUBLIC_EGRESS = "A2A_SPACE_REQUIRES_PUBLIC_EGRESS"


# ---------------------------------------------------------------------------
# 统一 discovered Agent 模型(hosted / external)
# ---------------------------------------------------------------------------


@dataclass
class DiscoveredAgent:
    """Space 中可发现的 Agent(hosted/external 统一模型,§3.2)。"""

    agent_id: str
    version_id: str
    source: str  # "hosted" | "external"
    agent_card: AgentCard
    credential_handle: Optional[str] = None
    etag: Optional[str] = None


@dataclass
class SpaceAgentPage:
    """ListA2ASpaceAgents 的一页结果。"""

    agents: list[DiscoveredAgent] = field(default_factory=list)
    etag: Optional[str] = None
    next_page_token: Optional[str] = None


# ---------------------------------------------------------------------------
# discovery backend
# ---------------------------------------------------------------------------


class SpaceDiscoveryBackend(ABC):
    """Space discovery 后端(§5.5 ``ListA2ASpaceAgents``)。"""

    @abstractmethod
    async def list_space_agents(
        self,
        space_id: str,
        *,
        prompt: Optional[str] = None,
        skill: Optional[str] = None,
        if_none_match: Optional[str] = None,
        page_number: Optional[int] = None,
        page_size: Optional[int] = None,
    ) -> SpaceAgentPage:
        raise NotImplementedError


class KopSpaceDiscoveryBackend(SpaceDiscoveryBackend):
    """经 KOP facade ``POST {base}/agentengine/api/v1/ListA2ASpaceAgents`` 的 HTTP 实现。

    §5.5:internal authenticated interface。服务端从 runtime identity 推导
    account/runtime_id 并验证 binding;本 client 经 ``headers`` 携带该 identity
    (由 gateway/STS 注入,不得伪造)。
    """

    def __init__(
        self,
        base_url: str,
        *,
        httpx_client: Optional[httpx.AsyncClient] = None,
        headers: Optional[dict[str, str]] = None,
        timeout: float = 15.0,
    ) -> None:
        if not base_url:
            raise ValueError(f"KopSpaceDiscoveryBackend 需要 base_url({ENV_A2A_SERVICE_URL})")
        self._base_url = base_url.rstrip("/")
        self._client = httpx_client
        self._headers = dict(headers or {})
        self._timeout = timeout

    async def list_space_agents(
        self,
        space_id: str,
        *,
        prompt: Optional[str] = None,
        skill: Optional[str] = None,
        if_none_match: Optional[str] = None,
        page_number: Optional[int] = None,
        page_size: Optional[int] = None,
    ) -> SpaceAgentPage:
        payload: dict[str, Any] = {"A2ASpaceId": space_id}
        if prompt:
            payload["Prompt"] = prompt
        if skill:
            payload["Skill"] = skill
        if if_none_match:
            payload["IfNoneMatch"] = if_none_match
        if page_number is not None:
            payload["PageNumber"] = page_number
        if page_size is not None:
            payload["PageSize"] = page_size

        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        own_client = self._client is None
        try:
            response = await client.post(
                f"{self._base_url}/agentengine/api/v1/ListA2ASpaceAgents",
                json=payload,
                headers=self._headers,
            )
            response.raise_for_status()
            envelope = response.json()
        finally:
            if own_client:
                await client.aclose()

        data = envelope.get("Data") or {}
        agents = [_discovered_agent_from_wire(item) for item in data.get("Agents") or []]
        return SpaceAgentPage(
            agents=agents,
            etag=data.get("ETag"),
            next_page_token=data.get("NextPageToken"),
        )


def _discovered_agent_from_wire(item: dict[str, Any]) -> DiscoveredAgent:
    """把 ListA2ASpaceAgents 的 wire 项解析为 DiscoveredAgent。

    §5.5 返回 latest AgentCard、agent/version ID、credential binding handle、ETag。
    """
    card_payload = item.get("AgentCard") or item.get("agent_card") or {}
    agent_card = _parse_agent_card(card_payload)
    return DiscoveredAgent(
        agent_id=str(item.get("AgentId") or item.get("agent_id") or ""),
        version_id=str(item.get("VersionId") or item.get("version_id") or ""),
        source=str(item.get("Source") or item.get("source") or "hosted").lower(),
        agent_card=agent_card,
        credential_handle=item.get("CredentialHandle") or item.get("credential_handle"),
        etag=item.get("ETag") or item.get("etag"),
    )


def _parse_agent_card(payload: dict[str, Any]) -> AgentCard:
    from google.protobuf.json_format import ParseDict

    card = ParseDict(payload, AgentCard())
    assert isinstance(card, AgentCard)
    return card


# ---------------------------------------------------------------------------
# A2ASpaceClient
# ---------------------------------------------------------------------------


class A2ASpaceClient:
    """Space 内动态发现与调用的 client(§3.2)。"""

    def __init__(
        self,
        space_id: str,
        backend: SpaceDiscoveryBackend,
        *,
        egress_enabled: bool = False,
        httpx_client: Optional[httpx.AsyncClient] = None,
        credential_provider: Optional[A2ACredentialProvider] = None,
        event_sink: Optional[Any] = None,
    ) -> None:
        self._space_id = space_id
        self._backend = backend
        self._egress_enabled = egress_enabled
        self._httpx_client = httpx_client
        self._credential_provider = credential_provider
        self._event_sink = event_sink
        self._event_adapter = A2AEventAdapter()
        self._agents_by_id: dict[str, DiscoveredAgent] = {}
        self._agents_by_task: dict[str, DiscoveredAgent] = {}
        self._seq = 0
        self._persisted_wire_events: set[str] = set()

    @classmethod
    def from_env(
        cls,
        *,
        backend: Optional[SpaceDiscoveryBackend] = None,
        httpx_client: Optional[httpx.AsyncClient] = None,
        egress_enabled: Optional[bool] = None,
        credential_provider: Optional[A2ACredentialProvider] = None,
        event_sink: Optional[Any] = None,
    ) -> "A2ASpaceClient":
        """从环境变量构造:``KSADK_A2A_SPACE_ID``(必需,兼容旧 ``AGENTENGINE_A2A_SPACE_ID``)
        + ``KSADK_A2A_SERVICE_URL``(可选;缺省经 AICP 自动探测,兼容旧 ``AGENTENGINE_SERVER_URL``)
        + ``KSADK_A2A_ENABLE_PUBLIC_EGRESS``(egress)。
        """
        space_id = str(
            os.getenv(ENV_A2A_SPACE_ID) or os.getenv(_LEGACY_ENV_A2A_SPACE_ID) or ""
        ).strip()
        if not space_id:
            raise ValueError(f"未设置 {ENV_A2A_SPACE_ID};Runtime 需先绑定 Space(§4.5 由部署层注入)")
        if backend is None:
            backend = KopSpaceDiscoveryBackend(
                _resolve_a2a_service_url(),
                httpx_client=httpx_client,
            )
        if egress_enabled is None:
            egress_enabled = (
                os.getenv(ENV_A2A_ENABLE_PUBLIC_EGRESS)
                or os.getenv(_LEGACY_ENV_A2A_ENABLE_PUBLIC_EGRESS)
                or ""
            ).strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
        return cls(
            space_id,
            backend,
            egress_enabled=egress_enabled,
            httpx_client=httpx_client,
            credential_provider=credential_provider,
            event_sink=event_sink,
        )

    # ---- discovery ----

    async def discover(
        self,
        prompt: Optional[str] = None,
        *,
        skill: Optional[str] = None,
    ) -> list[DiscoveredAgent]:
        """动态发现 Space 中 hosted/external Agent(§5.5 返回 latest AgentCard)。"""
        page = await self._backend.list_space_agents(self._space_id, prompt=prompt, skill=skill)
        for agent in page.agents:
            self._agents_by_id[agent.agent_id] = agent
        return page.agents

    # ---- egress ----

    def _check_egress(self, agent: DiscoveredAgent) -> None:
        """external 调用必须有公网出站;否则 §5.4 报错。"""
        if agent.source == "external" and not self._egress_enabled:
            raise PermissionError(
                f"{ERR_REQUIRES_PUBLIC_EGRESS}: agent {agent.agent_id} 为 external public,"
                " Runtime 未开启公网出站(§5.4)"
            )

    # ---- 调用 ----

    async def _resolve_agent(self, agent_id: str) -> DiscoveredAgent:
        agent = self._agents_by_id.get(agent_id)
        if agent is None:
            # 允许按需再发现一次(可能已经过期/未 discover 过)。
            await self.discover()
            agent = self._agents_by_id.get(agent_id)
        if agent is None:
            raise KeyError(f"Space {self._space_id} 中未发现 agent {agent_id!r}")
        self._check_egress(agent)
        return agent

    async def _client_for_agent(self, agent: DiscoveredAgent):
        # §3.2:按 agent 的 credential_handle 经 A2ACredentialProvider 解析出站凭据,
        # 注入 httpx 头(external 出站调用的真实鉴权,不再 read-but-unused)。
        headers: dict[str, str] = {}
        if self._credential_provider is not None:
            credential = await self._credential_provider.resolve(agent.credential_handle)
            headers = dict(credential.headers)
        if self._httpx_client is not None:
            httpx_client = self._httpx_client
        else:
            httpx_client = httpx.AsyncClient()
        client = await create_client(
            agent=agent.agent_card,
            client_config=ClientConfig(httpx_client=httpx_client, streaming=True),
        )
        call_context = ClientCallContext(service_parameters=headers or None)
        return client, httpx_client, call_context

    async def send_message(
        self,
        agent_id: str,
        message: str | Message,
        *,
        return_immediately: bool = False,
    ):
        """向 Space 中某 Agent 发送消息(§3.2),返回首个 Task(或 terminal 结果)。"""
        agent = await self._resolve_agent(agent_id)
        client, httpx_client, call_context = await self._client_for_agent(agent)
        try:
            if isinstance(message, str):
                message = Message(
                    role=Role.ROLE_USER,
                    parts=[Part(text=message)],
                    message_id=f"sm-{uuid.uuid4().hex}",
                )
            elif not getattr(message, "message_id", ""):
                message.message_id = f"sm-{uuid.uuid4().hex}"
            request = SendMessageRequest(
                message=message,
                configuration=SendMessageConfiguration(return_immediately=return_immediately),
            )
            first_task = None
            wire_position = 0
            async for response in client.send_message(request, context=call_context):
                if response.task and response.task.id:
                    if first_task is None:
                        first_task = response.task
                        self._agents_by_task[first_task.id] = agent
                        await self._record_agent_for_task(first_task.id, agent)
                await self._persist_events(
                    self._stream_item_to_events(response, agent, wire_position=wire_position)
                )
                wire_position += 1
                if return_immediately and first_task is not None:
                    break
            if first_task is not None:
                self._agents_by_task[first_task.id] = agent
                await self._record_agent_for_task(first_task.id, agent)
            return first_task
        finally:
            if self._httpx_client is None:
                await client.close()
                await httpx_client.aclose()

    async def subscribe(self, task_id: str):
        """Subscribe raw SDK items while durably projecting every item."""
        agent = self._agents_by_task.get(task_id) or await self._resolve_agent_for_task(task_id)
        async for item, _ in self._iter_subscription(task_id, agent):
            yield item

    async def _iter_subscription(self, task_id: str, agent: DiscoveredAgent):
        client, httpx_client, call_context = await self._client_for_agent(agent)
        try:
            wire_position = 0
            async for event in client.subscribe(
                SubscribeToTaskRequest(id=task_id), context=call_context
            ):
                persisted = await self._persist_events(
                    self._stream_item_to_events(event, agent, wire_position=wire_position)
                )
                wire_position += 1
                yield event, persisted
        finally:
            if self._httpx_client is None:
                await client.close()
                await httpx_client.aclose()

    async def cancel(self, task_id: str):
        """取消 task(§3.2)。"""
        agent = self._agents_by_task.get(task_id) or await self._resolve_agent_for_task(task_id)
        client, httpx_client, call_context = await self._client_for_agent(agent)
        try:
            return await client.cancel_task(CancelTaskRequest(id=task_id), context=call_context)
        finally:
            if self._httpx_client is None:
                await client.close()
                await httpx_client.aclose()

    async def get_task(self, task_id: str):
        agent = self._agents_by_task.get(task_id) or await self._resolve_agent_for_task(task_id)
        client, httpx_client, call_context = await self._client_for_agent(agent)
        try:
            return await client.get_task(GetTaskRequest(id=task_id), context=call_context)
        finally:
            if self._httpx_client is None:
                await client.close()
                await httpx_client.aclose()

    # ---- 出站结果 → RuntimeEvent(§3.2 A2AEventAdapter) ----

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _event_ctx(
        self,
        agent: DiscoveredAgent,
        invocation_id: str,
        *,
        event_id: Optional[str] = None,
    ) -> dict[str, Any]:
        return {
            "agent_id": agent.agent_id,
            "user_id": "a2a_space",
            "session_id": self._space_id,
            "invocation_id": invocation_id,
            "seq_id": self._next_seq(),
            "event_id": event_id,
        }

    def task_to_event(self, task: Any, agent: DiscoveredAgent) -> RuntimeEvent:
        """A2A Task → RuntimeEvent(run.*)(经 A2AEventAdapter,出站不再裸返回)。"""
        return self._event_adapter.task_status_to_event(
            task.status, **self._event_ctx(agent, invocation_id=str(task.id))
        )

    def _stream_item_to_events(
        self,
        item: Any,
        agent: DiscoveredAgent,
        *,
        wire_position: int = 0,
    ) -> list[RuntimeEvent]:
        """把 subscribe 流的一个包装项(.task/.status_update/.artifact_update/.message)
        经 A2AEventAdapter 转成 0..n 个 RuntimeEvent。"""
        events: list[RuntimeEvent] = []
        task = getattr(item, "task", None)
        status_update = getattr(item, "status_update", None)
        artifact_update = getattr(item, "artifact_update", None)
        message = getattr(item, "message", None)
        invocation_id = str(
            getattr(item, "task_id", None)
            or getattr(task, "id", "")
            or getattr(status_update, "task_id", "")
            or getattr(artifact_update, "task_id", "")
            or getattr(message, "task_id", "")
            or ""
        )

        def ctx(kind: str, value: Any) -> dict[str, Any]:
            metadata = getattr(value, "metadata", None)
            native_event_id = ""
            if metadata is not None:
                if isinstance(metadata, Mapping):
                    metadata_dict = dict(metadata)
                else:
                    try:
                        metadata_dict = MessageToDict(metadata, preserving_proto_field_name=True)
                    except (AttributeError, TypeError, ValueError):
                        metadata_dict = {}
                native_event_id = str(
                    metadata_dict.get("event_id") or metadata_dict.get("ksadk_event_id") or ""
                )
            message_id = str(getattr(value, "message_id", "") or "")
            artifact = getattr(value, "artifact", None)
            artifact_id = str(
                getattr(value, "artifact_id", "") or getattr(artifact, "artifact_id", "") or ""
            )
            source_id = native_event_id or message_id or artifact_id
            event_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"ksadk:a2a:{invocation_id}:{wire_position}:{kind}:{source_id}",
            ).hex
            return self._event_ctx(agent, invocation_id=invocation_id, event_id=event_id)

        if task is not None and getattr(task, "status", None) is not None:
            events.append(
                self._event_adapter.task_status_to_event(task.status, **ctx("task", task))
            )
        if status_update is not None and getattr(status_update, "status", None) is not None:
            status_message = getattr(status_update.status, "message", None)
            text = A2AEventAdapter._parts_text(getattr(status_message, "parts", None))
            terminal_states = {
                TaskState.TASK_STATE_COMPLETED,
                TaskState.TASK_STATE_FAILED,
                TaskState.TASK_STATE_CANCELED,
                TaskState.TASK_STATE_REJECTED,
            }
            is_terminal = status_update.status.state in terminal_states
            if not is_terminal:
                events.append(
                    self._event_adapter.task_status_to_event(
                        status_update.status, **ctx("status", status_update)
                    )
                )
            if text:
                events.append(
                    self._event_adapter.message_to_event(
                        text,
                        final=is_terminal,
                        **ctx("status-message", status_message),
                    )
                )
            if is_terminal:
                events.append(
                    self._event_adapter.task_status_to_event(
                        status_update.status, **ctx("status", status_update)
                    )
                )
        if artifact_update is not None and getattr(artifact_update, "artifact", None) is not None:
            events.append(
                self._event_adapter.artifact_to_event(
                    artifact_update.artifact, **ctx("artifact", artifact_update)
                )
            )
        if message is not None:
            text = A2AEventAdapter._parts_text(getattr(message, "parts", None))
            if text:
                events.append(
                    self._event_adapter.message_to_event(
                        text, final=True, **ctx("message", message)
                    )
                )
        return events

    async def _persist_events(self, events: list[RuntimeEvent]) -> list[RuntimeEvent]:
        existing_ids = set(self._persisted_wire_events)
        if self._event_sink is not None:
            list_events = getattr(self._event_sink, "list", None)
            if callable(list_events) and events:
                persisted_before = await list_events(events[0].session_id)
                existing_ids.update(event.event_id for event in persisted_before)
        fresh = [event for event in events if event.event_id not in existing_ids]
        if not fresh:
            return []
        if self._event_sink is not None:
            append = getattr(self._event_sink, "append", None)
            if append is None:
                raise TypeError("event_sink 必须提供 async append(events)")
            persisted = await append(fresh)
            if persisted is not None:
                fresh = list(persisted)
        self._persisted_wire_events.update(event.event_id for event in fresh)
        return fresh

    async def subscribe_events(self, task_id: str):
        """订阅 task 事件流并逐个转成 RuntimeEvent(§3.2 出站经 A2AEventAdapter)。"""
        agent = self._agents_by_task.get(task_id) or await self._resolve_agent_for_task(task_id)
        async for _, persisted in self._iter_subscription(task_id, agent):
            for event in persisted:
                yield event

    async def subscribe_persisted_events(
        self,
        *,
        after_seq_id: int = 0,
        timeout: float = 1.0,
    ) -> AsyncIterator[RuntimeEvent]:
        """从注入的 RuntimeEventStore 按 cursor 续传本 Space 的出站事件。"""
        subscribe = getattr(self._event_sink, "subscribe_session", None)
        if subscribe is None:
            raise RuntimeError("event_sink 不支持 subscribe_session cursor replay")
        async for event in subscribe(
            self._space_id,
            after_seq_id=after_seq_id,
            timeout=timeout,
        ):
            yield event

    async def _resolve_agent_for_task(self, task_id: str) -> DiscoveredAgent:
        """Resolve task ownership from the durable locator, then discovery."""
        get_task_agent = getattr(self._event_sink, "get_task_agent", None)
        if callable(get_task_agent):
            agent_id = await get_task_agent(self._space_id, task_id)
            if agent_id:
                agent = await self._resolve_agent(str(agent_id))
                self._agents_by_task[task_id] = agent
                return agent
        raise KeyError(f"未知 task {task_id!r} 所属 agent;缺少持久化 task locator")

    async def _record_agent_for_task(self, task_id: str, agent: DiscoveredAgent) -> None:
        set_task_agent = getattr(self._event_sink, "set_task_agent", None)
        if callable(set_task_agent):
            await set_task_agent(self._space_id, task_id, agent.agent_id)


__all__ = [
    "A2ASpaceClient",
    "DiscoveredAgent",
    "ENV_A2A_ENABLE_PUBLIC_EGRESS",
    "ENV_A2A_SERVICE_URL",
    "ENV_A2A_SPACE_ID",
    "ERR_REQUIRES_PUBLIC_EGRESS",
    "KopSpaceDiscoveryBackend",
    "SpaceAgentPage",
    "SpaceDiscoveryBackend",
]
