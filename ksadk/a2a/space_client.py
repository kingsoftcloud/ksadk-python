"""Space-scoped A2A discovery and authorized data-plane calls."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
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
)
from google.protobuf.json_format import MessageToDict, ParseDict

from ksadk.a2a._space_client_events import _SpaceClientEventMixin
from ksadk.a2a.control_plane import (
    A2AAgentCardClient,
    A2AControlPlane,
    A2ARouteInterface,
    CredentialInjection,
    DiscoveredAgent,
    PreparedA2AOperation,
    SpaceAgentPage,
)
from ksadk.a2a.event_adapter import A2AEventAdapter
from ksadk.a2a.external_transport import A2AExternalTransport
from ksadk.a2a.ids import require_a2a_resource_id
from ksadk.a2a.task_event_dispatcher import A2ATaskEventDispatcher
from ksadk.a2a.task_event_outbox import (
    A2ATaskEventOutbox,
    InMemoryA2ATaskEventOutbox,
    SQLiteA2ATaskEventOutbox,
)
from ksadk.events.runtime_event import RuntimeEvent

logger = logging.getLogger(__name__)

ENV_A2A_SPACE_ID = "KSADK_A2A_SPACE_ID"
ENV_A2A_SPACE_IDS = "KSADK_A2A_SPACE_IDS"
ENV_A2A_ENABLE_PUBLIC_EGRESS = "KSADK_A2A_ENABLE_PUBLIC_EGRESS"

ERR_PUBLIC_EGRESS_DISABLED = "A2A_PUBLIC_EGRESS_DISABLED"
MAX_A2A_MESSAGE_BYTES = 1024 * 1024
MAX_A2A_MESSAGE_PARTS = 64
MAX_A2A_MESSAGE_ID_LENGTH = 128


def _require_opaque_space_id(value: str, *, field_name: str) -> str:
    """Validate a server-issued opaque Space ID without assuming a prefix."""
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 256 or any(char.isspace() for char in normalized):
        raise ValueError(f"{field_name} must be a non-empty opaque resource ID")
    return normalized


@dataclass(frozen=True)
class A2APlatformTask:
    """AgentEngine task locator with an optional latest remote A2A snapshot."""

    id: str
    remote_task: Any | None = None


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


class A2ASpaceClient(_SpaceClientEventMixin):
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
        event_dispatcher: A2ATaskEventDispatcher | None = None,
    ) -> None:
        space_id = _require_opaque_space_id(space_id, field_name="space_id")
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
        if event_dispatcher is not None and event_outbox is not None:
            raise ValueError("pass either event_dispatcher or event_outbox, not both")
        self._owns_event_dispatcher = event_dispatcher is None
        self._event_dispatcher = event_dispatcher or A2ATaskEventDispatcher(
            event_outbox or InMemoryA2ATaskEventOutbox(),
            backend,
        )
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
        event_dispatcher: A2ATaskEventDispatcher | None = None,
    ) -> "A2ASpaceClient":
        selected_space_id = str(space_id or os.getenv(ENV_A2A_SPACE_ID) or "").strip()
        if selected_space_id:
            selected_space_id = _require_opaque_space_id(selected_space_id, field_name="space_id")
        else:
            raw_space_ids = str(os.getenv(ENV_A2A_SPACE_IDS) or "").strip()
            if not raw_space_ids:
                raise ValueError(
                    f"missing {ENV_A2A_SPACE_ID} and {ENV_A2A_SPACE_IDS}; pass space_id "
                    "or add the Runtime Agent to an A2A Space first"
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
                normalized = _require_opaque_space_id(
                    normalized, field_name=f"{ENV_A2A_SPACE_IDS}[{index}]"
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
            from ksadk.a2a.service_env import (
                resolve_a2a_service_token,
                resolve_a2a_service_url,
            )

            service_url = resolve_a2a_service_url()
            if not service_url:
                raise ValueError(
                    "A2A service url not configured: set KSADK_A2A_SERVICE_URL or AICP env"
                )
            backend = A2AAgentCardClient(
                service_url,
                service_token=resolve_a2a_service_token(),
                httpx_client=httpx_client,
            )
        if egress_enabled is None:
            raw_egress = os.getenv(ENV_A2A_ENABLE_PUBLIC_EGRESS) or ""
            egress_enabled = raw_egress.strip().lower() in {"1", "true", "yes", "on"}
        if event_outbox is None and event_dispatcher is None:
            event_outbox = SQLiteA2ATaskEventOutbox()
        return cls(
            selected_space_id,
            backend,
            egress_enabled=egress_enabled,
            httpx_client=httpx_client,
            external_transport=external_transport,
            event_sink=event_sink,
            event_outbox=event_outbox,
            event_dispatcher=event_dispatcher,
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

    @property
    def event_dispatcher(self) -> A2ATaskEventDispatcher:
        """Runtime-scoped task event dispatcher used by this client."""

        return self._event_dispatcher

    async def aclose(self, *, flush_timeout_seconds: float = 5.0) -> None:
        """Stop the dispatcher only when this standalone client created it."""

        if self._owns_event_dispatcher:
            await self._event_dispatcher.stop(flush_timeout_seconds=flush_timeout_seconds)

    async def __aenter__(self) -> "A2ASpaceClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

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
        first_task = None
        remote_task_id = prepared.remote_task.remote_task_id if prepared.remote_task else None
        remote_context_id = prepared.remote_task.remote_context_id if prepared.remote_task else None
        operation_instance_id = self._operation_instance_id(prepared)
        async with self._operation_client(agent, prepared) as (client, context):
            request = SendMessageRequest(
                message=message,
                configuration=SendMessageConfiguration(return_immediately=return_immediately),
            )
            wire_position = 0
            async for response in client.send_message(request, context=context):
                response_task = _present_message_field(response, "task")
                if response_task is not None and str(getattr(response_task, "id", None) or ""):
                    # 跟踪最新 task 状态：每个带 task 的 response 都更新 first_task，
                    # 使 send_message 返回的是最终状态(如 COMPLETED)而非首个 SUBMITTED。
                    first_task = response_task
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
        async with self._operation_client(agent, prepared) as (client, context):
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
        async with self._operation_client(agent, prepared) as (client, context):
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

    async def get_task(self, task_id: str) -> A2APlatformTask:
        require_a2a_resource_id(task_id, "a2a-task-", field_name="task_id")
        cached_agent = self._agents_by_task.get(task_id)
        prepared = await self._backend.prepare_task_operation(
            platform_task_id=task_id,
            operation="get_task",
            agent_id=getattr(cached_agent, "agent_id", None),
        )
        self._validate_prepared_ids(prepared)
        remote_task_ref = self._require_remote_task(prepared)
        agent = self._agent_from_prepared(prepared)
        async with self._operation_client(agent, prepared) as (client, context):
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

    @asynccontextmanager
    async def _operation_client(
        self,
        agent: DiscoveredAgent,
        prepared: PreparedA2AOperation,
    ) -> AsyncIterator[tuple[Any, ClientCallContext]]:
        # Product bootstrap starts the shared dispatcher during lifespan startup.
        # Standalone/from_env clients own their dispatcher and must start it here.
        await self._event_dispatcher.start()
        injection = CredentialInjection()
        headers: dict[str, str]
        async with AsyncExitStack() as exit_stack:
            if prepared.route.kind == "hosted_gateway":
                token = self._backend.gateway_token()
                headers = {
                    "X-AgentEngine-A2A-Permit": prepared.call_permit,
                }
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                http = self._httpx_client
            else:
                if prepared.route.kind == "external_public" and not self._egress_enabled:
                    raise PermissionError(ERR_PUBLIC_EGRESS_DISABLED)
                if self._external_transport is None:
                    raise RuntimeError(
                        "A2A_EGRESS_TRANSPORT_REQUIRED: external calls require a Runtime network "
                        "guard transport"
                    )
                lease = await exit_stack.enter_async_context(
                    self._external_transport.open_for_route(
                        prepared.route.interface,
                        route_kind=prepared.route.kind,
                    )
                )
                http = lease.httpx_client
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
            if http is None:
                # 外部调用方需自行传入已配置 verify 的 httpx_client。
                owned_http = httpx.AsyncClient(trust_env=False)
                http = owned_http
            client = await create_client(
                agent=route_card,
                client_config=ClientConfig(httpx_client=http, streaming=True),
            )
            try:
                yield client, ClientCallContext(service_parameters=headers or None)
            finally:
                if owned_http is not None:
                    await client.close()
                    await owned_http.aclose()

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

    async def _bind_task(self, platform_task_id: str, remote_task: Any) -> None:
        await self._backend.bind_remote_task(
            platform_task_id=platform_task_id,
            remote_task_id=str(remote_task.id),
            remote_context_id=str(remote_task.context_id or "") or None,
            observed_at=_utc_now(),
        )

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
    "ENV_A2A_SPACE_ID",
    "ENV_A2A_SPACE_IDS",
    "ERR_PUBLIC_EGRESS_DISABLED",
    "SpaceAgentPage",
]
