"""AgentEngine product composition root for managed inbound and outbound A2A."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Protocol, Sequence
from urllib.parse import urlsplit

import httpx
from a2a.server.tasks import TaskStore
from a2a.types import AgentSkill
from fastapi import FastAPI

from ksadk.a2a.context_store import A2AContextStore
from ksadk.a2a.external_transport import RuntimeLocalA2AExternalTransport
from ksadk.a2a.identity import (
    A2AGatewayIdentityMiddleware,
    A2AIngressTargetBinding,
    A2ATrustedIdentityResolver,
    GatewayIdentityVerifier,
    GatewayProbeVerifier,
)
from ksadk.a2a.ids import require_a2a_resource_id
from ksadk.a2a.resume_store import A2AResumeStateStore
from ksadk.a2a.routes import A2AConfig, add_a2a_protocol_routes
from ksadk.a2a.task_adapter import A2ARuntimeTaskAdapter
from ksadk.a2a.task_event_dispatcher import A2ATaskEventDispatcher
from ksadk.a2a.task_event_outbox import A2ATaskEventOutbox
from ksadk.a2a.task_store import A2AOwnerContextBuilder
from ksadk.runtime.adapter import RuntimeAdapter


@dataclass(frozen=True)
class RuntimeA2AMetadata:
    account_id: str
    tenant_id: str
    agent_id: str
    a2a_agent_id: str
    runtime_id: str
    internal_base_url: str
    name: str
    version: str
    skills: Sequence[AgentSkill]
    description: str = ""

    def validate(self) -> None:
        values = {
            "account_id": self.account_id,
            "agent_id": self.agent_id,
            "a2a_agent_id": self.a2a_agent_id,
            "runtime_id": self.runtime_id,
            "internal_base_url": self.internal_base_url,
            "name": self.name,
            "version": self.version,
        }
        missing = [name for name, value in values.items() if not str(value).strip()]
        if missing:
            raise ValueError(f"RuntimeA2AMetadata is missing required fields: {', '.join(missing)}")
        if not self.agent_id.startswith("ar-"):
            raise ValueError("RuntimeA2AMetadata.agent_id must be an ar-* Agent ID")
        require_a2a_resource_id(
            self.a2a_agent_id,
            "a2a-agent-",
            field_name="RuntimeA2AMetadata.a2a_agent_id",
        )
        for field_name, limit in (
            ("account_id", 64),
            ("tenant_id", 64),
            ("agent_id", 64),
            ("a2a_agent_id", 64),
            ("runtime_id", 64),
            ("name", 128),
            ("description", 1024),
            ("version", 64),
        ):
            if len(str(getattr(self, field_name))) > limit:
                raise ValueError(f"RuntimeA2AMetadata.{field_name} exceeds {limit} characters")
        if len(self.skills) > 100 or not all(
            isinstance(skill, AgentSkill) for skill in self.skills
        ):
            raise ValueError(
                "RuntimeA2AMetadata.skills must contain at most 100 AgentSkill objects"
            )
        parsed = urlsplit(self.internal_base_url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError(
                "RuntimeA2AMetadata.internal_base_url must be an absolute HTTP(S) origin"
            ) from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.netloc.rsplit("@", 1)[-1].endswith(":")
            or port is not None and not 1 <= port <= 65535
        ):
            raise ValueError(
                "RuntimeA2AMetadata.internal_base_url must be an absolute HTTP(S) origin"
            )


class A2ACheckpointStore(Protocol):
    """Durable checkpoint backend readiness contract owned by the Runtime adapter."""

    async def initialize(self) -> None: ...


class AgentEngineA2ABootstrap:
    """Single product-owned composition root for managed A2A Runtime dependencies."""

    def __init__(
        self,
        *,
        runtime_metadata: RuntimeA2AMetadata,
        task_store: TaskStore | None = None,
        context_store: A2AContextStore | None = None,
        checkpoint_store: A2ACheckpointStore | None = None,
        resume_state_store: A2AResumeStateStore | None = None,
        gateway_identity_verifier: GatewayIdentityVerifier | None = None,
        gateway_probe_verifier: GatewayProbeVerifier | None = None,
        external_transport: RuntimeLocalA2AExternalTransport | None = None,
        control_plane: Any | None = None,
        hosted_http_client: httpx.AsyncClient | None = None,
        event_outbox: A2ATaskEventOutbox | None = None,
        inbound_enabled: bool = True,
        outbound_enabled: bool = True,
        public_egress_enabled: bool = False,
        outbox_retry_interval_seconds: float = 1.0,
    ) -> None:
        runtime_metadata.validate()
        required: dict[str, Any] = {}
        if outbound_enabled:
            required.update(
                {
                    "control_plane": control_plane,
                    "hosted_http_client": hosted_http_client,
                    "event_outbox": event_outbox,
                }
            )
        if inbound_enabled:
            required.update(
                {
                    "task_store": task_store,
                    "context_store": context_store,
                    "checkpoint_store": checkpoint_store,
                    "resume_state_store": resume_state_store,
                    "gateway_identity_verifier": gateway_identity_verifier,
                    "gateway_probe_verifier": gateway_probe_verifier,
                }
            )
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ValueError(
                "AgentEngineA2ABootstrap requires: " + ", ".join(missing)
            )
        if external_transport is not None and not isinstance(
            external_transport, RuntimeLocalA2AExternalTransport
        ):
            raise TypeError(
                "AgentEngineA2ABootstrap requires RuntimeLocalA2AExternalTransport "
                "for external_public routes"
            )
        if not outbound_enabled and external_transport is not None:
            raise ValueError("external_transport requires outbound_enabled=True")
        if inbound_enabled:
            _require_initializable(task_store, "task_store")
            _require_initializable(context_store, "context_store")
            _require_initializable(checkpoint_store, "checkpoint_store")
            _require_initializable(resume_state_store, "resume_state_store")
        self.runtime_metadata = runtime_metadata
        self._target_binding = A2AIngressTargetBinding(
            account_id=runtime_metadata.account_id,
            tenant_id=runtime_metadata.tenant_id,
            agent_id=runtime_metadata.agent_id,
            runtime_id=runtime_metadata.runtime_id,
            a2a_agent_id=runtime_metadata.a2a_agent_id,
        )
        self.inbound_enabled = inbound_enabled
        self.outbound_enabled = outbound_enabled
        self.public_egress_enabled = public_egress_enabled
        self._task_store = task_store
        self._context_store = context_store
        self._checkpoint_store = checkpoint_store
        self._resume_state_store = resume_state_store
        self._gateway_identity_verifier = gateway_identity_verifier
        self._gateway_probe_verifier = gateway_probe_verifier
        self._external_transport = external_transport
        self._control_plane = control_plane
        self._hosted_http_client = hosted_http_client
        self._dispatcher = (
            A2ATaskEventDispatcher(
                _require(event_outbox, "event_outbox"),
                _require(control_plane, "control_plane"),
                retry_interval_seconds=outbox_retry_interval_seconds,
            )
            if outbound_enabled
            else None
        )
        self._mounted = False
        self._server: Any = None

    @classmethod
    def from_platform(cls, **kwargs: Any) -> "AgentEngineA2ABootstrap":
        """Explicit product factory; dependencies are injected by the platform layer."""

        return cls(**kwargs)

    @property
    def event_dispatcher(self) -> A2ATaskEventDispatcher:
        if self._dispatcher is None:
            raise RuntimeError("A2A outbound client is disabled for this Runtime")
        return self._dispatcher

    @property
    def server(self) -> Any:
        return self._server

    def client_for_space(self, space_id: str):
        """Create a Space-scoped client sharing Runtime identity, clients, and outbox."""

        from ksadk.a2a.space_client import A2ASpaceClient

        if not self.outbound_enabled:
            raise RuntimeError("A2A outbound client is disabled for this Runtime")
        return A2ASpaceClient(
            space_id,
            _require(self._control_plane, "control_plane"),
            egress_enabled=self.public_egress_enabled,
            httpx_client=_require(self._hosted_http_client, "hosted_http_client"),
            external_transport=self._external_transport,
            event_dispatcher=self.event_dispatcher,
        )

    def mount(
        self,
        app: FastAPI,
        *,
        runner: Any,
        runtime_adapter: RuntimeAdapter,
        runtime_type: str,
    ) -> Any | None:
        """Mount exactly one managed inbound A2A server into an app."""

        if self._mounted:
            raise RuntimeError("AgentEngineA2ABootstrap may only be mounted once")
        self._mounted = True
        app.state.a2a_bootstrap = self
        if not self.inbound_enabled:
            return None
        app.add_middleware(
            A2AGatewayIdentityMiddleware,
            verifier=_require(self._gateway_identity_verifier, "gateway_identity_verifier"),
            probe_verifier=_require(self._gateway_probe_verifier, "gateway_probe_verifier"),
            target_binding=self._target_binding,
        )
        config = A2AConfig(
            enabled=True,
            base_url=self.runtime_metadata.internal_base_url,
            agent_name=self.runtime_metadata.name,
            description=self.runtime_metadata.description,
            version=self.runtime_metadata.version,
            skills=self.runtime_metadata.skills,
            create_table=False,
        )
        task_adapter = A2ARuntimeTaskAdapter(
            runtime_adapter,
            runtime_type=runtime_type,
            context_store=_require(self._context_store, "context_store"),
            resume_state_store=_require(self._resume_state_store, "resume_state_store"),
        )
        self._server = add_a2a_protocol_routes(
            app,
            runner,
            config,
            task_adapter=task_adapter,
            task_store=_require(self._task_store, "task_store"),
            context_builder=A2AOwnerContextBuilder(
                identity_resolver=A2ATrustedIdentityResolver(
                    target_binding=self._target_binding,
                ),
                allow_unverified_identity=False,
            ),
        )
        return self._server

    async def start(self) -> None:
        if self.inbound_enabled:
            await _initialize_required(_require(self._task_store, "task_store"), "task_store")
            await _initialize_required(
                _require(self._context_store, "context_store"), "context_store"
            )
            await _initialize_required(
                _require(self._checkpoint_store, "checkpoint_store"), "checkpoint_store"
            )
            await _initialize_required(
                _require(self._resume_state_store, "resume_state_store"),
                "resume_state_store",
            )
        if self._dispatcher is not None:
            await self._dispatcher.start()

    async def stop(self, *, flush_timeout_seconds: float = 5.0) -> None:
        if self._dispatcher is not None:
            await self._dispatcher.stop(flush_timeout_seconds=flush_timeout_seconds)


def _require_initializable(value: Any, name: str) -> None:
    initialize = getattr(value, "initialize", None)
    if not callable(initialize):
        raise TypeError(f"{name} must implement async initialize()")


async def _initialize_required(value: Any, name: str) -> None:
    _require_initializable(value, name)
    initialize = value.initialize
    result = initialize()
    if not inspect.isawaitable(result):
        raise TypeError(f"{name}.initialize() must return an awaitable")
    await result


def _require(value: Any, name: str) -> Any:
    if value is None:
        raise RuntimeError(f"AgentEngineA2ABootstrap is missing {name}")
    return value


__all__ = ["A2ACheckpointStore", "AgentEngineA2ABootstrap", "RuntimeA2AMetadata"]
