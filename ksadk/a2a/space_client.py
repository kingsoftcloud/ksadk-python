"""Space-scoped A2A discovery and authorized data-plane calls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
from google.protobuf.json_format import MessageToDict, ParseDict

from ksadk.a2a.control_plane import (
    ENV_A2A_CONTROL_PLANE_URL,
    A2AControlPlane,
    A2ARouteInterface,
    CredentialInjection,
    DiscoveredAgent,
    InternalA2AControlPlaneClient,
    PreparedA2AOperation,
    SpaceAgentPage,
)
from ksadk.a2a.event_adapter import A2AEventAdapter
from ksadk.a2a.ids import require_a2a_resource_id
from ksadk.a2a.task_event_outbox import (
    A2ATaskEventOutbox,
    InMemoryA2ATaskEventOutbox,
    SQLiteA2ATaskEventOutbox,
)
from ksadk.events.runtime_event import RuntimeEvent

logger = logging.getLogger(__name__)

ENV_A2A_SPACE_IDS = "KSADK_A2A_SPACE_IDS"
ENV_A2A_ENABLE_PUBLIC_EGRESS = "KSADK_A2A_ENABLE_PUBLIC_EGRESS"

ERR_PUBLIC_EGRESS_DISABLED = "A2A_PUBLIC_EGRESS_DISABLED"
MAX_A2A_MESSAGE_BYTES = 1024 * 1024
MAX_A2A_MESSAGE_PARTS = 64
MAX_A2A_MESSAGE_ID_LENGTH = 128


@dataclass(frozen=True)
class A2APlatformTask:
    """AgentEngine task locator with an optional latest remote A2A snapshot."""

    id: str
    remote_task: Any | None = None


class A2AExternalTransport(ABC):
    """Capability object supplied by the Runtime network guard for external routes."""

    @abstractmethod
    def client_for_route(
        self,
        route: A2ARouteInterface,
        *,
        route_kind: str,
    ) -> httpx.AsyncClient:
        """Return a client whose DNS/IP/redirect policy is validated and pinned for the route."""
        raise NotImplementedError


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_proto(value: Any) -> dict[str, Any]:
    return MessageToDict(value, preserving_proto_field_name=False)


def _canonical_sha256(value: Any) -> str:
    payload = _canonical_proto(value)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _present_message_field(value: Any, field_name: str) -> Any | None:
    has_field = getattr(value, "HasField", None)
    if callable(has_field):
        try:
            if not has_field(field_name):
                return None
        except ValueError:
            pass
    return getattr(value, field_name, None)


class A2ASpaceClient:
    """Discovers Space members and performs permit-authorized A2A calls."""

    def __init__(
        self,
        space_id: str,
        backend: A2AControlPlane,
        *,
        egress_enabled: bool = False,
        httpx_client: httpx.AsyncClient | None = None,
        external_transport: A2AExternalTransport | None = None,
        event_sink: Any | None = None,
        event_outbox: A2ATaskEventOutbox | None = None,
    ) -> None:
        require_a2a_resource_id(space_id, "a2a-space-", field_name="space_id")
        if external_transport is not None and not isinstance(
            external_transport, A2AExternalTransport
        ):
            raise TypeError("external_transport must implement A2AExternalTransport")
        self._space_id = space_id
        self._backend = backend
        self._egress_enabled = egress_enabled
        self._httpx_client = httpx_client
        self._external_transport = external_transport
        self._event_sink = event_sink
        self._event_outbox = event_outbox or InMemoryA2ATaskEventOutbox()
        self._outbox_flush_lock = asyncio.Lock()
        self._event_adapter = A2AEventAdapter()
        self._agents_by_id: dict[str, DiscoveredAgent] = {}
        self._agents_by_task: dict[str, DiscoveredAgent] = {}
        self._seq = 0
        self._persisted_wire_events: set[str] = set()

    @classmethod
    def from_env(
        cls,
        *,
        space_id: str | None = None,
        backend: A2AControlPlane | None = None,
        httpx_client: httpx.AsyncClient | None = None,
        external_transport: A2AExternalTransport | None = None,
        egress_enabled: bool | None = None,
        event_sink: Any | None = None,
        event_outbox: A2ATaskEventOutbox | None = None,
    ) -> "A2ASpaceClient":
        selected_space_id = str(space_id or "").strip()
        if selected_space_id:
            require_a2a_resource_id(
                selected_space_id,
                "a2a-space-",
                field_name="space_id",
            )
        else:
            raw_space_ids = str(os.getenv(ENV_A2A_SPACE_IDS) or "").strip()
            if not raw_space_ids:
                raise ValueError(
                    f"missing {ENV_A2A_SPACE_IDS}; pass space_id or add the Runtime Agent "
                    "to an A2A Space first"
                )
            try:
                configured_space_ids = json.loads(raw_space_ids)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{ENV_A2A_SPACE_IDS} must be a JSON string array") from exc
            if not isinstance(configured_space_ids, list) or not configured_space_ids:
                raise ValueError(f"{ENV_A2A_SPACE_IDS} must be a non-empty JSON string array")
            if len(configured_space_ids) > 100:
                raise ValueError(f"{ENV_A2A_SPACE_IDS} cannot contain more than 100 Space IDs")
            normalized_space_ids: list[str] = []
            for index, configured_space_id in enumerate(configured_space_ids):
                if not isinstance(configured_space_id, str):
                    raise ValueError(f"{ENV_A2A_SPACE_IDS}[{index}] must be an A2A Space ID string")
                normalized = configured_space_id.strip()
                require_a2a_resource_id(
                    normalized,
                    "a2a-space-",
                    field_name=f"{ENV_A2A_SPACE_IDS}[{index}]",
                )
                normalized_space_ids.append(normalized)
            if len(set(normalized_space_ids)) != len(normalized_space_ids):
                raise ValueError(f"{ENV_A2A_SPACE_IDS} must not contain duplicate Space IDs")
            if len(normalized_space_ids) != 1:
                raise ValueError(
                    f"{ENV_A2A_SPACE_IDS} contains multiple Space IDs; pass space_id explicitly"
                )
            selected_space_id = normalized_space_ids[0]
        if backend is None:
            control_plane_url = str(os.getenv(ENV_A2A_CONTROL_PLANE_URL) or "").strip()
            if not control_plane_url:
                raise ValueError(f"missing {ENV_A2A_CONTROL_PLANE_URL}")
            backend = InternalA2AControlPlaneClient(
                control_plane_url,
                httpx_client=httpx_client,
            )
        if egress_enabled is None:
            raw_egress = os.getenv(ENV_A2A_ENABLE_PUBLIC_EGRESS) or ""
            egress_enabled = raw_egress.strip().lower() in {"1", "true", "yes", "on"}
        if event_outbox is None:
            event_outbox = SQLiteA2ATaskEventOutbox()
        return cls(
            selected_space_id,
            backend,
            egress_enabled=egress_enabled,
            httpx_client=httpx_client,
            external_transport=external_transport,
            event_sink=event_sink,
            event_outbox=event_outbox,
        )

    async def discover(
        self,
        prompt: str | None = None,
        *,
        skill: str | None = None,
        include_blocked: bool = False,
    ) -> list[DiscoveredAgent]:
        page = await self._backend.list_space_agents(
            self._space_id,
            prompt=prompt,
            skill_id=skill,
            include_blocked=include_blocked,
        )
        for agent in page.agents:
            require_a2a_resource_id(
                agent.agent_id,
                "a2a-agent-",
                field_name="DiscoveredAgent.agent_id",
            )
            require_a2a_resource_id(
                agent.version_id,
                "a2a-version-",
                field_name="DiscoveredAgent.version_id",
            )
            self._agents_by_id[agent.agent_id] = agent
        return page.agents

    def _check_egress(self, agent: DiscoveredAgent) -> None:
        if agent.route_kind == "external_public" and not self._egress_enabled:
            raise PermissionError(
                f"{ERR_PUBLIC_EGRESS_DISABLED}: external Agent {agent.agent_id} requires "
                "Network.EnablePublicAccess"
            )

    async def _resolve_agent(self, agent_id: str) -> DiscoveredAgent:
        agent = self._agents_by_id.get(agent_id)
        if agent is None:
            await self.discover()
            agent = self._agents_by_id.get(agent_id)
        if agent is None:
            raise KeyError(f"Agent {agent_id!r} is not discoverable in Space {self._space_id}")
        self._check_egress(agent)
        return agent

    async def send_message(
        self,
        agent_id: str,
        message: str | Message,
        *,
        return_immediately: bool = False,
        idempotency_token: str | None = None,
    ) -> A2APlatformTask:
        agent = await self._resolve_agent(agent_id)
        normalized = self._normalize_initial_message(message)
        prepared = await self._backend.prepare_call(
            space_id=self._space_id,
            target_agent_id=agent.agent_id,
            expected_version_id=agent.version_id,
            message_id=normalized.message_id,
            message_sha256=_canonical_sha256(normalized),
            idempotency_token=idempotency_token or f"idem-{uuid.uuid4().hex}",
        )
        self._validate_prepared_target(agent, prepared)
        handle = await self._send_prepared_message(
            prepared,
            agent,
            normalized,
            return_immediately=return_immediately,
        )
        self._agents_by_task[prepared.platform_task_id] = agent
        await self._record_agent_for_task(prepared.platform_task_id, agent)
        return handle

    async def continue_task(
        self,
        task_id: str,
        message: str | Message,
        *,
        return_immediately: bool = False,
        idempotency_token: str | None = None,
    ) -> A2APlatformTask:
        require_a2a_resource_id(task_id, "a2a-task-", field_name="task_id")
        normalized = self._normalize_initial_message(message)
        prepared = await self._backend.prepare_task_operation(
            platform_task_id=task_id,
            operation="send_message",
            message_id=normalized.message_id,
            message_sha256=_canonical_sha256(normalized),
            idempotency_token=idempotency_token or f"idem-{uuid.uuid4().hex}",
        )
        self._validate_prepared_ids(prepared)
        remote_task = self._require_remote_task(prepared)
        normalized.task_id = remote_task.remote_task_id
        if remote_task.remote_context_id:
            normalized.context_id = remote_task.remote_context_id
        agent = self._agent_from_prepared(prepared)
        return await self._send_prepared_message(
            prepared,
            agent,
            normalized,
            return_immediately=return_immediately,
        )

    async def _send_prepared_message(
        self,
        prepared: PreparedA2AOperation,
        agent: DiscoveredAgent,
        message: Message,
        *,
        return_immediately: bool,
    ) -> A2APlatformTask:
        client, owned_http, context = await self._client_for_operation(agent, prepared)
        first_task = None
        remote_task_id = prepared.remote_task.remote_task_id if prepared.remote_task else None
        remote_context_id = prepared.remote_task.remote_context_id if prepared.remote_task else None
        operation_instance_id = self._operation_instance_id(prepared)
        try:
            request = SendMessageRequest(
                message=message,
                configuration=SendMessageConfiguration(return_immediately=return_immediately),
            )
            wire_position = 0
            async for response in client.send_message(request, context=context):
                response_task = _present_message_field(response, "task")
                if response_task is not None and str(getattr(response_task, "id", None) or ""):
                    first_task = first_task or response_task
                    observed_task_id = str(response_task.id)
                    observed_context_id = str(response_task.context_id or "") or None
                    if remote_task_id is None:
                        await self._bind_task(prepared.platform_task_id, response_task)
                    elif (remote_task_id, remote_context_id) != (
                        observed_task_id,
                        observed_context_id,
                    ):
                        raise RuntimeError("A2A_REMOTE_BINDING_CONFLICT")
                    remote_task_id = observed_task_id
                    remote_context_id = observed_context_id
                if remote_task_id is not None:
                    self._validate_remote_task_observation(
                        remote_task_id,
                        remote_context_id,
                        response,
                    )
                await self._project_stream_item(
                    prepared.platform_task_id,
                    response,
                    agent,
                    wire_position=wire_position,
                    operation_instance_id=operation_instance_id,
                )
                wire_position += 1
                if return_immediately and first_task is not None:
                    break
        finally:
            await self._close_operation_client(client, owned_http)
        return A2APlatformTask(
            id=prepared.platform_task_id,
            remote_task=first_task,
        )

    async def subscribe(self, task_id: str):
        require_a2a_resource_id(task_id, "a2a-task-", field_name="task_id")
        prepared = await self._backend.prepare_task_operation(
            platform_task_id=task_id,
            operation="subscribe_to_task",
        )
        self._validate_prepared_ids(prepared)
        agent = self._agent_from_prepared(prepared)
        async for item, _ in self._iter_subscription(prepared, agent):
            yield item

    async def _iter_subscription(
        self,
        prepared: PreparedA2AOperation,
        agent: DiscoveredAgent,
    ):
        remote_task = self._require_remote_task(prepared)
        operation_instance_id = self._operation_instance_id(prepared)
        client, owned_http, context = await self._client_for_operation(agent, prepared)
        try:
            wire_position = 0
            async for event in client.subscribe(
                SubscribeToTaskRequest(id=remote_task.remote_task_id),
                context=context,
            ):
                self._validate_remote_task_observation(
                    remote_task.remote_task_id,
                    remote_task.remote_context_id,
                    event,
                )
                persisted = await self._project_stream_item(
                    prepared.platform_task_id,
                    event,
                    agent,
                    wire_position=wire_position,
                    operation_instance_id=operation_instance_id,
                )
                wire_position += 1
                yield event, persisted
        finally:
            await self._close_operation_client(client, owned_http)

    async def cancel(
        self,
        task_id: str,
        *,
        idempotency_token: str | None = None,
    ) -> A2APlatformTask:
        require_a2a_resource_id(task_id, "a2a-task-", field_name="task_id")
        prepared = await self._backend.prepare_task_operation(
            platform_task_id=task_id,
            operation="cancel_task",
            idempotency_token=idempotency_token or f"idem-{uuid.uuid4().hex}",
        )
        self._validate_prepared_ids(prepared)
        remote_task_ref = self._require_remote_task(prepared)
        agent = self._agent_from_prepared(prepared)
        client, owned_http, context = await self._client_for_operation(agent, prepared)
        try:
            remote_task = await client.cancel_task(
                CancelTaskRequest(id=remote_task_ref.remote_task_id), context=context
            )
            self._validate_remote_task_observation(
                remote_task_ref.remote_task_id,
                remote_task_ref.remote_context_id,
                remote_task,
            )
            await self._project_stream_item(
                task_id,
                remote_task,
                agent,
                wire_position=0,
                operation_instance_id=self._operation_instance_id(prepared),
            )
            return A2APlatformTask(
                id=task_id,
                remote_task=remote_task,
            )
        finally:
            await self._close_operation_client(client, owned_http)

    async def get_task(self, task_id: str) -> A2APlatformTask:
        require_a2a_resource_id(task_id, "a2a-task-", field_name="task_id")
        prepared = await self._backend.prepare_task_operation(
            platform_task_id=task_id,
            operation="get_task",
        )
        self._validate_prepared_ids(prepared)
        remote_task_ref = self._require_remote_task(prepared)
        agent = self._agent_from_prepared(prepared)
        client, owned_http, context = await self._client_for_operation(agent, prepared)
        try:
            remote_task = await client.get_task(
                GetTaskRequest(id=remote_task_ref.remote_task_id), context=context
            )
            self._validate_remote_task_observation(
                remote_task_ref.remote_task_id,
                remote_task_ref.remote_context_id,
                remote_task,
            )
            await self._project_stream_item(
                task_id,
                remote_task,
                agent,
                wire_position=0,
                operation_instance_id=self._operation_instance_id(prepared),
            )
            return A2APlatformTask(
                id=task_id,
                remote_task=remote_task,
            )
        finally:
            await self._close_operation_client(client, owned_http)

    def _normalize_initial_message(self, message: str | Message) -> Message:
        if isinstance(message, str):
            message = Message(
                role=Role.ROLE_USER,
                parts=[Part(text=message)],
                message_id=f"message-{uuid.uuid4().hex}",
            )
        if getattr(message, "task_id", "") or getattr(message, "context_id", ""):
            raise ValueError("caller must not provide remote task_id/context_id")
        if message.role != Role.ROLE_USER:
            raise ValueError("A2A Message role must be user")
        if not 1 <= len(message.parts) <= MAX_A2A_MESSAGE_PARTS:
            raise ValueError("A2A Message parts must contain 1-64 items")
        if not getattr(message, "message_id", ""):
            message.message_id = f"message-{uuid.uuid4().hex}"
        if len(message.message_id) > MAX_A2A_MESSAGE_ID_LENGTH:
            raise ValueError("A2A Message message_id must contain 1-128 characters")
        encoded = json.dumps(
            _canonical_proto(message),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_A2A_MESSAGE_BYTES:
            raise ValueError("A2A Message canonical JSON exceeds 1 MiB")
        return message

    @staticmethod
    def _validate_prepared_target(
        agent: DiscoveredAgent,
        prepared: PreparedA2AOperation,
    ) -> None:
        A2ASpaceClient._validate_prepared_ids(prepared)
        if prepared.target.agent_id != agent.agent_id:
            raise RuntimeError("PrepareA2ACall returned a different target Agent")
        if prepared.target.version_id != agent.version_id:
            raise RuntimeError("PrepareA2ACall returned a different target version")
        if agent.card_sha256 and prepared.target.card_sha256 != agent.card_sha256:
            raise RuntimeError("PrepareA2ACall returned a different AgentCard hash")

    @staticmethod
    def _validate_prepared_ids(prepared: PreparedA2AOperation) -> None:
        require_a2a_resource_id(
            prepared.platform_task_id,
            "a2a-task-",
            field_name="PreparedA2AOperation.platform_task_id",
        )
        require_a2a_resource_id(
            prepared.target.agent_id,
            "a2a-agent-",
            field_name="PreparedA2AOperation.target.agent_id",
        )
        require_a2a_resource_id(
            prepared.target.version_id,
            "a2a-version-",
            field_name="PreparedA2AOperation.target.version_id",
        )

    @staticmethod
    def _require_remote_task(prepared: PreparedA2AOperation):
        remote_task = prepared.remote_task
        if remote_task is None or not remote_task.remote_task_id:
            raise RuntimeError("A2A_REMOTE_TASK_NOT_BOUND")
        return remote_task

    @staticmethod
    def _operation_instance_id(prepared: PreparedA2AOperation) -> str:
        return hashlib.sha256(prepared.call_permit.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_remote_task_observation(
        expected_task_id: str,
        expected_context_id: str | None,
        item: Any,
    ) -> None:
        task = _present_message_field(item, "task")
        if task is None and hasattr(item, "status") and hasattr(item, "id"):
            task = item
        status_update = _present_message_field(item, "status_update")
        artifact_update = _present_message_field(item, "artifact_update")
        message = _present_message_field(item, "message")
        candidate = task or status_update or artifact_update or message
        if candidate is None:
            return
        observed_task_id = str(
            getattr(candidate, "id", "") or getattr(candidate, "task_id", "") or ""
        )
        observed_context_id = str(getattr(candidate, "context_id", "") or "") or None
        if observed_task_id and observed_task_id != expected_task_id:
            raise RuntimeError("A2A_REMOTE_BINDING_CONFLICT")
        if observed_context_id != expected_context_id:
            raise RuntimeError("A2A_REMOTE_BINDING_CONFLICT")

    def _agent_from_prepared(self, prepared: PreparedA2AOperation) -> DiscoveredAgent:
        cached = self._agents_by_id.get(prepared.target.agent_id)
        if cached is not None and cached.version_id == prepared.target.version_id:
            return cached
        card = self._route_only_card(prepared.target.agent_id, prepared.target.version_id)
        return DiscoveredAgent(
            agent_id=prepared.target.agent_id,
            version_id=prepared.target.version_id,
            source="hosted" if prepared.route.kind == "hosted_gateway" else "external",
            agent_card=card,
            card_sha256=prepared.target.card_sha256,
            route_kind=prepared.route.kind,
        )

    def _route_only_card(self, name: str, version: str) -> AgentCard:
        return ParseDict(
            {
                "name": name,
                "description": "AgentEngine prepared A2A route",
                "version": version,
                "supportedInterfaces": [],
                "capabilities": {},
                "defaultInputModes": ["text/plain"],
                "defaultOutputModes": ["text/plain"],
                "skills": [],
            },
            AgentCard(),
        )

    async def _client_for_operation(
        self,
        agent: DiscoveredAgent,
        prepared: PreparedA2AOperation,
    ):
        await self._event_outbox.initialize()
        await self._flush_pending_platform_events(raise_on_error=False)
        injection = CredentialInjection()
        headers: dict[str, str]
        external_http: httpx.AsyncClient | None = None
        if prepared.route.kind == "hosted_gateway":
            headers = {
                "Authorization": f"Bearer {self._backend.gateway_token()}",
                "X-AgentEngine-A2A-Permit": prepared.call_permit,
            }
        else:
            if prepared.route.kind == "external_public" and not self._egress_enabled:
                raise PermissionError(ERR_PUBLIC_EGRESS_DISABLED)
            if self._external_transport is None:
                raise RuntimeError(
                    "A2A_EGRESS_TRANSPORT_REQUIRED: external calls require a Runtime network "
                    "guard transport"
                )
            external_http = self._external_transport.client_for_route(
                prepared.route.interface,
                route_kind=prepared.route.kind,
            )
            if not isinstance(external_http, httpx.AsyncClient):
                raise TypeError("A2AExternalTransport must return httpx.AsyncClient")
            injection = await self._backend.resolve_credential(
                platform_task_id=prepared.platform_task_id,
                credential_handle=prepared.credential_handle,
                call_permit=prepared.call_permit,
            )
            headers = dict(injection.headers)
            if injection.cookies:
                if any(name.lower() == "cookie" for name in headers):
                    raise RuntimeError(
                        "A2A_CREDENTIAL_INJECTION_CONFLICT: Cookie header and cookie injection "
                        "cannot both be present"
                    )
                headers["Cookie"] = self._cookie_header(injection.cookies)
        route = prepared.route.interface
        if injection.query:
            route = A2ARouteInterface(
                url=self._url_with_query(route.url, injection.query),
                protocol_binding=route.protocol_binding,
                protocol_version=route.protocol_version,
            )
        route_card = self._card_for_route(agent.agent_card, route)
        owned_http = None
        if prepared.route.kind == "hosted_gateway":
            http = self._httpx_client
        else:
            http = external_http
            assert http is not None
        if http is None:
            owned_http = httpx.AsyncClient()
            http = owned_http
        client = await create_client(
            agent=route_card,
            client_config=ClientConfig(httpx_client=http, streaming=True),
        )
        return client, owned_http, ClientCallContext(service_parameters=headers or None)

    @staticmethod
    def _url_with_query(url: str, query: dict[str, str]) -> str:
        parsed = urlsplit(url)
        values = parse_qsl(parsed.query, keep_blank_values=True)
        existing = {name for name, _ in values}
        collision = existing.intersection(query)
        if collision:
            raise RuntimeError(
                "A2A_CREDENTIAL_INJECTION_CONFLICT: credential query collides with route query: "
                f"{sorted(collision)}"
            )
        values.extend(query.items())
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urlencode(values), parsed.fragment)
        )

    @staticmethod
    def _cookie_header(cookies: dict[str, str]) -> str:
        jar = SimpleCookie()
        try:
            for name, value in cookies.items():
                jar[name] = value
        except CookieError as exc:
            raise RuntimeError(
                "A2A_CREDENTIAL_INJECTION_CONFLICT: invalid credential cookie"
            ) from exc
        return jar.output(header="", sep="; ").strip()

    @staticmethod
    def _card_for_route(card: AgentCard, route: A2ARouteInterface) -> AgentCard:
        payload = _canonical_proto(card)
        payload["supportedInterfaces"] = [
            {
                "url": route.url,
                "protocolBinding": route.protocol_binding,
                "protocolVersion": route.protocol_version,
            }
        ]
        return ParseDict(payload, AgentCard())

    async def _close_operation_client(
        self,
        client: Any,
        owned_http: httpx.AsyncClient | None,
    ) -> None:
        if owned_http is not None:
            await client.close()
            await owned_http.aclose()

    async def _bind_task(self, platform_task_id: str, remote_task: Any) -> None:
        await self._backend.bind_remote_task(
            platform_task_id=platform_task_id,
            remote_task_id=str(remote_task.id),
            remote_context_id=str(remote_task.context_id or "") or None,
            observed_at=_utc_now(),
        )

    async def _project_stream_item(
        self,
        platform_task_id: str,
        item: Any,
        agent: DiscoveredAgent,
        *,
        wire_position: int,
        operation_instance_id: str,
    ) -> list[RuntimeEvent]:
        runtime_events = self._stream_item_to_events(
            item,
            agent,
            wire_position=wire_position,
            invocation_id=platform_task_id,
        )
        persisted = await self._persist_events(runtime_events)
        platform_events = self._platform_events(
            item,
            platform_task_id,
            operation_instance_id=operation_instance_id,
            wire_position=wire_position,
        )
        if platform_events:
            await self._event_outbox.enqueue(
                platform_task_id=platform_task_id,
                events=platform_events,
            )
            await self._flush_pending_platform_events(raise_on_error=False)
        return persisted

    def _platform_events(
        self,
        item: Any,
        platform_task_id: str,
        *,
        operation_instance_id: str,
        wire_position: int,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []

        def append_event(
            kind: str,
            payload: dict[str, Any],
            *,
            status: str | None = None,
            occurred_at: str | None = None,
        ) -> None:
            events.append(
                self._platform_event(
                    kind,
                    payload,
                    platform_task_id,
                    operation_instance_id=operation_instance_id,
                    wire_position=wire_position,
                    event_ordinal=len(events),
                    status=status,
                    occurred_at=occurred_at,
                )
            )

        task = _present_message_field(item, "task")
        if task is None and hasattr(item, "status") and hasattr(item, "id"):
            task = item
        status_update = _present_message_field(item, "status_update")
        artifact_update = _present_message_field(item, "artifact_update")
        message = _present_message_field(item, "message")
        if task is not None and getattr(task, "status", None) is not None:
            payload = _canonical_proto(task.status)
            state_name = TaskState.Name(task.status.state)
            append_event(
                "status",
                payload,
                status=state_name.removeprefix("TASK_STATE_").lower(),
                occurred_at=str(payload.get("timestamp") or _utc_now()),
            )
            for artifact in getattr(task, "artifacts", None) or []:
                append_event(
                    "artifact",
                    {
                        "Artifact": _canonical_proto(artifact),
                        "Append": False,
                        "LastChunk": True,
                    },
                )
        if status_update is not None and getattr(status_update, "status", None) is not None:
            payload = _canonical_proto(status_update.status)
            state_name = TaskState.Name(status_update.status.state)
            append_event(
                "status",
                payload,
                status=state_name.removeprefix("TASK_STATE_").lower(),
                occurred_at=str(payload.get("timestamp") or _utc_now()),
            )
        if artifact_update is not None and getattr(artifact_update, "artifact", None) is not None:
            append_event(
                "artifact",
                {
                    "Artifact": _canonical_proto(artifact_update.artifact),
                    "Append": bool(getattr(artifact_update, "append", False)),
                    "LastChunk": bool(getattr(artifact_update, "last_chunk", False)),
                },
            )
        if message is not None:
            payload = _canonical_proto(message)
            append_event("message", payload)
            append_event(
                "status",
                {"state": "TASK_STATE_COMPLETED", "message": payload},
                status="completed",
            )
        return events

    @staticmethod
    def _platform_event(
        kind: str,
        payload: dict[str, Any],
        platform_task_id: str,
        *,
        operation_instance_id: str,
        wire_position: int,
        event_ordinal: int,
        status: str | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        source_id = hashlib.sha256(
            (
                f"{platform_task_id}:{operation_instance_id}:{wire_position}:{event_ordinal}:{kind}"
            ).encode("utf-8")
        ).hexdigest()
        event: dict[str, Any] = {
            "SourceEventId": source_id,
            "EventKind": kind,
            "Payload": payload,
            "OccurredAt": occurred_at or _utc_now(),
        }
        if status:
            event["Status"] = status
        return event

    async def flush_pending_events(self) -> int:
        """Deliver all currently queued platform event batches or raise on failure."""

        await self._event_outbox.initialize()
        return await self._flush_pending_platform_events(raise_on_error=True)

    async def _flush_pending_platform_events(self, *, raise_on_error: bool) -> int:
        delivered = 0
        async with self._outbox_flush_lock:
            while True:
                batches = await self._event_outbox.pending(limit=100)
                if not batches:
                    return delivered
                for batch in batches:
                    try:
                        await self._backend.append_task_events(
                            platform_task_id=batch.platform_task_id,
                            events=batch.events,
                        )
                    except Exception as exc:
                        failure = str(getattr(exc, "error_code", "") or type(exc).__name__)
                        await self._event_outbox.record_failure(batch.batch_id, failure)
                        if raise_on_error:
                            raise
                        logger.warning(
                            "A2A task event batch remains in local outbox: task=%s batch=%s",
                            batch.platform_task_id,
                            batch.batch_id,
                        )
                        return delivered
                    await self._event_outbox.acknowledge(batch.batch_id)
                    delivered += 1

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _event_ctx(
        self,
        agent: DiscoveredAgent,
        invocation_id: str,
        *,
        event_id: str | None = None,
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
        return self._event_adapter.task_status_to_event(
            task.status, **self._event_ctx(agent, invocation_id=str(task.id))
        )

    def _stream_item_to_events(
        self,
        item: Any,
        agent: DiscoveredAgent,
        *,
        wire_position: int = 0,
        invocation_id: str | None = None,
    ) -> list[RuntimeEvent]:
        events: list[RuntimeEvent] = []
        task = _present_message_field(item, "task")
        if task is None and hasattr(item, "status") and hasattr(item, "id"):
            task = item
        status_update = _present_message_field(item, "status_update")
        artifact_update = _present_message_field(item, "artifact_update")
        message = _present_message_field(item, "message")
        resolved_invocation_id = invocation_id or str(
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
                f"ksadk:a2a:{resolved_invocation_id}:{wire_position}:{kind}:{source_id}",
            ).hex
            return self._event_ctx(agent, invocation_id=resolved_invocation_id, event_id=event_id)

        if task is not None and getattr(task, "status", None) is not None:
            task_status_message = _present_message_field(task.status, "message")
            task_status_text = A2AEventAdapter._parts_text(
                getattr(task_status_message, "parts", None)
            )
            task_is_terminal = task.status.state in {
                TaskState.TASK_STATE_COMPLETED,
                TaskState.TASK_STATE_FAILED,
                TaskState.TASK_STATE_CANCELED,
                TaskState.TASK_STATE_REJECTED,
            }
            if not task_is_terminal:
                events.append(
                    self._event_adapter.task_status_to_event(
                        task.status,
                        **ctx("task", task),
                    )
                )
            if task_status_text:
                events.append(
                    self._event_adapter.message_to_event(
                        task_status_text,
                        final=task_is_terminal,
                        **ctx("task-status-message", task_status_message),
                    )
                )
            if task_is_terminal:
                events.append(
                    self._event_adapter.task_status_to_event(
                        task.status,
                        **ctx("task", task),
                    )
                )
        if status_update is not None and getattr(status_update, "status", None) is not None:
            status_message = _present_message_field(status_update.status, "message")
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
            artifact = artifact_update.artifact
            events.append(
                self._event_adapter.artifact_to_event(artifact, **ctx("artifact", artifact_update))
            )
            artifact_text = A2AEventAdapter._parts_text(getattr(artifact, "parts", None))
            if artifact_text and str(getattr(artifact, "name", "") or "") == "response":
                events.append(
                    self._event_adapter.message_to_event(
                        artifact_text,
                        final=bool(getattr(artifact_update, "last_chunk", False)),
                        **ctx("artifact-text", artifact_update),
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
                raise TypeError("event_sink must provide async append(events)")
            persisted = await append(fresh)
            if persisted is not None:
                fresh = list(persisted)
        self._persisted_wire_events.update(event.event_id for event in fresh)
        return fresh

    async def subscribe_events(self, task_id: str):
        require_a2a_resource_id(task_id, "a2a-task-", field_name="task_id")
        prepared = await self._backend.prepare_task_operation(
            platform_task_id=task_id,
            operation="subscribe_to_task",
        )
        self._validate_prepared_ids(prepared)
        agent = self._agent_from_prepared(prepared)
        async for _, persisted in self._iter_subscription(prepared, agent):
            for event in persisted:
                yield event

    async def subscribe_persisted_events(
        self,
        *,
        after_seq_id: int = 0,
        timeout: float = 1.0,
    ) -> AsyncIterator[RuntimeEvent]:
        subscribe = getattr(self._event_sink, "subscribe_session", None)
        if subscribe is None:
            raise RuntimeError("event_sink does not support subscribe_session cursor replay")
        async for event in subscribe(
            self._space_id,
            after_seq_id=after_seq_id,
            timeout=timeout,
        ):
            yield event

    async def _record_agent_for_task(self, task_id: str, agent: DiscoveredAgent) -> None:
        set_task_agent = getattr(self._event_sink, "set_task_agent", None)
        if callable(set_task_agent):
            await set_task_agent(self._space_id, task_id, agent.agent_id)


__all__ = [
    "A2AExternalTransport",
    "A2APlatformTask",
    "A2ASpaceClient",
    "DiscoveredAgent",
    "ENV_A2A_ENABLE_PUBLIC_EGRESS",
    "ENV_A2A_SPACE_IDS",
    "ERR_PUBLIC_EGRESS_DISABLED",
    "SpaceAgentPage",
]
