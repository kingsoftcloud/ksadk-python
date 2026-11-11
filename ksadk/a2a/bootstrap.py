"""AgentEngine product composition root for managed inbound and outbound A2A."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
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
from ksadk.a2a.product_kernel_facts import ProductKernelFactSink, ProductKernelReconciler
from ksadk.a2a.task_event_outbox import A2ATaskEventOutbox
from ksadk.a2a.task_store import A2AOwnerContextBuilder
from ksadk.a2a.tool_activity import PublicToolActivityPolicy
from ksadk.managed_a2a_card import ManagedA2ACardConfig, ManagedA2ACardMount
from ksadk.runtime.adapter import RuntimeAdapter

MANAGED_MODEL_READ_TIMEOUT_SECONDS = 300.0


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

    def validate(self, *, inbound_enabled: bool = True) -> None:
        values = {
            "account_id": self.account_id,
            "agent_id": self.agent_id,
            "a2a_agent_id": self.a2a_agent_id,
            "runtime_id": self.runtime_id,
            "internal_base_url": self.internal_base_url,
            "name": self.name,
            "version": self.version,
        }
        inbound_fields = {"a2a_agent_id", "internal_base_url", "name", "version"}
        missing = [
            name
            for name, value in values.items()
            if not str(value).strip() and (inbound_enabled or name not in inbound_fields)
        ]
        if missing:
            raise ValueError(f"RuntimeA2AMetadata is missing required fields: {', '.join(missing)}")
        if not self.agent_id.startswith("ar-"):
            raise ValueError("RuntimeA2AMetadata.agent_id must be an ar-* Agent ID")
        if inbound_enabled or self.a2a_agent_id:
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
        if not inbound_enabled and not self.internal_base_url:
            return
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
            or port is not None
            and not 1 <= port <= 65535
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
        invocation_checkpoint_store: Any | None = None,
        public_tool_policy: PublicToolActivityPolicy | None = None,
        inbound_enabled: bool = True,
        outbound_enabled: bool = True,
        public_egress_enabled: bool = False,
        outbox_retry_interval_seconds: float = 1.0,
        managed_resource_closers: Sequence[Callable[[], Awaitable[None]]] = (),
    ) -> None:
        runtime_metadata.validate(inbound_enabled=inbound_enabled)
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
            raise ValueError("AgentEngineA2ABootstrap requires: " + ", ".join(missing))
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
        self.public_tool_policy = public_tool_policy or PublicToolActivityPolicy()
        self._target_binding = (
            A2AIngressTargetBinding(
                account_id=runtime_metadata.account_id,
                tenant_id=runtime_metadata.tenant_id,
                agent_id=runtime_metadata.agent_id,
                runtime_id=runtime_metadata.runtime_id,
                a2a_agent_id=runtime_metadata.a2a_agent_id,
            )
            if inbound_enabled
            else None
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
        self._invocation_checkpoint_store = invocation_checkpoint_store
        self._hosted_http_client = hosted_http_client
        self._dispatcher = (
            A2ATaskEventDispatcher(
                _require(event_outbox, "event_outbox"),
                ProductKernelFactSink(_require(control_plane, "control_plane")),
                retry_interval_seconds=outbox_retry_interval_seconds,
            )
            if outbound_enabled or (inbound_enabled and event_outbox is not None)
            else None
        )
        self._kernel_reconciler = None
        self._mounted = False
        self._server: Any = None
        self._managed_resource_closers = tuple(managed_resource_closers)

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

    @property
    def invocation_checkpoint_store(self):
        return _require(self._invocation_checkpoint_store, "invocation_checkpoint_store")

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

    @property
    def scoped_cancel_available(self):
        return self.outbound_enabled and self._invocation_checkpoint_store is not None

    def invocation_service_for_space(
        self, space_id: str, *, event_store, policy, recover_checkpoint_leases=False,
    ):
        """Product-owned caller service; policy and canonical ownership stay explicit."""
        from ksadk.a2a.invocation_service import A2AInvocationService
        from ksadk.a2a.timeouts import managed_invocation_timeouts

        return A2AInvocationService.from_space_client(
            self.client_for_space(space_id),
            event_store=event_store,
            policy=policy,
            checkpoint_store=self.invocation_checkpoint_store,
            recover_checkpoint_leases=recover_checkpoint_leases,
            # A model may be silent between status/tool events. Match the
            # managed data-plane transport ceiling; _timed still caps every
            # wait at the invocation's original absolute deadline.
            timeouts=managed_invocation_timeouts(),
        )

    def mount(
        self,
        app: FastAPI,
        *,
        runtime_adapter: RuntimeAdapter,
        runtime_type: str,
    ) -> Any | None:
        """Mount exactly one managed inbound A2A server into an app."""

        if self._mounted:
            raise RuntimeError("AgentEngineA2ABootstrap may only be mounted once")
        self._mounted = True
        app.state.a2a_bootstrap = self
        if not self.inbound_enabled:
            ManagedA2ACardMount(
                ManagedA2ACardConfig(
                    base_url=self.runtime_metadata.internal_base_url
                    or "http://localhost:8080",
                    agent_name=self.runtime_metadata.name or self.runtime_metadata.runtime_id,
                    description=self.runtime_metadata.description,
                    version=self.runtime_metadata.version or "0.1.0",
                    skills=self.runtime_metadata.skills,
                )
            ).mount(app)
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
            public_tool_policy=self.public_tool_policy,
        )
        from ksadk.kernel.ingress import authority_mode, kernel_ingress_enabled
        task_adapter_class = A2ARuntimeTaskAdapter
        task_adapter_options = {}
        if kernel_ingress_enabled() and authority_mode() == "hosted":
            from ksadk.a2a.kernel_task_adapter import ManagedKernelA2ATaskAdapter
            self._kernel_reconciler = ProductKernelReconciler(self._control_plane, _ManagedKernelEvents())
            task_adapter_class = ManagedKernelA2ATaskAdapter
            task_adapter_options = {"control_plane": _require(self._control_plane, "control_plane"),
                                    "session_events": _ManagedKernelEvents(),
                                    "event_dispatcher": self._dispatcher}
        task_adapter = task_adapter_class(
            runtime_adapter,
            runtime_type=runtime_type,
            context_store=_require(self._context_store, "context_store"),
            resume_state_store=_require(self._resume_state_store, "resume_state_store"),
            **task_adapter_options,
        )
        self._server = add_a2a_protocol_routes(
            app,
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
        # Probe evidence for the platform's registration reconciler. This is
        # public identity, not an authorization credential; the probe itself
        # still passes through Gateway identity verification.
        from a2a.types import AgentExtension
        from google.protobuf.json_format import ParseDict
        from google.protobuf.struct_pb2 import Struct

        self._server.agent_card.capabilities.extensions.append(
            AgentExtension(
                uri="urn:ksadk:a2a:managed-runtime:v1",
                required=False,
                params=ParseDict(
                    {
                        "agent_id": self.runtime_metadata.agent_id,
                        "registration_id": self.runtime_metadata.a2a_agent_id,
                        "runtime_id": self.runtime_metadata.runtime_id,
                        "version": self.runtime_metadata.version,
                    },
                    Struct(),
                ),
            )
        )
        return self._server

    async def start(self) -> None:
        if self.outbound_enabled and self._invocation_checkpoint_store is not None:
            await _initialize_required(
                self._invocation_checkpoint_store, "invocation_checkpoint_store"
            )
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
        if self._kernel_reconciler is not None:
            await self._kernel_reconciler.start()

    async def stop(self, *, flush_timeout_seconds: float = 5.0) -> None:
        try:
            if self._kernel_reconciler is not None:
                await self._kernel_reconciler.stop()
            if self._dispatcher is not None:
                await self._dispatcher.stop(flush_timeout_seconds=flush_timeout_seconds)
        finally:
            for closer in self._managed_resource_closers:
                await asyncio.wait_for(closer(), timeout=max(0.1, flush_timeout_seconds))


class _ManagedKernelEvents:
    """Resolve the Kernel after app lifespan startup, never fall back to an adapter."""

    async def subscribe(self, session_id, after_seq):
        from ksadk.kernel.bootstrap import get_agent_kernel_runtime
        from ksadk.a2a.inbound_kernel import ManagedKernelRequestError
        runtime = get_agent_kernel_runtime()
        if runtime is None:
            raise ManagedKernelRequestError("managed A2A Kernel is not ready")
        try:
            subscribe = getattr(runtime.session_events, "subscribe_recovering", None)
            if not callable(subscribe):
                subscribe = runtime.session_events.subscribe
            async for fact in subscribe(session_id, after_seq):
                yield fact
        except (OSError, TimeoutError) as exc:
            raise ManagedKernelRequestError("Kernel event observation was interrupted") from exc


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
