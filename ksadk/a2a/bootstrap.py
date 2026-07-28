"""AgentEngine product composition root for managed inbound and outbound A2A."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Sequence

import httpx
from a2a.server.tasks import TaskStore
from fastapi import FastAPI

from ksadk.a2a.context_store import A2AContextStore
from ksadk.a2a.external_transport import A2AExternalTransport
from ksadk.a2a.identity import (
    A2AGatewayIdentityMiddleware,
    A2ATrustedIdentityResolver,
    GatewayIdentityVerifier,
)
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
    runtime_id: str
    internal_base_url: str
    name: str
    version: str
    skills: Sequence[str]
    description: str = ""

    def validate(self) -> None:
        values = {
            "account_id": self.account_id,
            "agent_id": self.agent_id,
            "runtime_id": self.runtime_id,
            "internal_base_url": self.internal_base_url,
            "name": self.name,
            "version": self.version,
        }
        missing = [name for name, value in values.items() if not str(value).strip()]
        if missing:
            raise ValueError(f"RuntimeA2AMetadata is missing required fields: {', '.join(missing)}")
        if not self.internal_base_url.startswith(("http://", "https://")):
            raise ValueError(
                "RuntimeA2AMetadata.internal_base_url must be an absolute HTTP(S) origin"
            )


class AgentEngineA2ABootstrap:
    """Single product-owned composition root for managed A2A Runtime dependencies."""

    def __init__(
        self,
        *,
        runtime_metadata: RuntimeA2AMetadata,
        task_store: TaskStore | None,
        context_store: A2AContextStore | None,
        checkpoint_store: Any | None,
        gateway_identity_verifier: GatewayIdentityVerifier | None,
        external_transport: A2AExternalTransport,
        control_plane: Any,
        hosted_http_client: httpx.AsyncClient,
        event_outbox: A2ATaskEventOutbox,
        inbound_enabled: bool = True,
        public_egress_enabled: bool = True,
        outbox_retry_interval_seconds: float = 1.0,
    ) -> None:
        runtime_metadata.validate()
        required = {
            "external_transport": external_transport,
            "control_plane": control_plane,
            "hosted_http_client": hosted_http_client,
            "event_outbox": event_outbox,
        }
        if inbound_enabled:
            required.update(
                {
                    "task_store": task_store,
                    "context_store": context_store,
                    "checkpoint_store": checkpoint_store,
                    "gateway_identity_verifier": gateway_identity_verifier,
                }
            )
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ValueError(
                "AgentEngineA2ABootstrap requires: " + ", ".join(missing)
            )
        self.runtime_metadata = runtime_metadata
        self.inbound_enabled = inbound_enabled
        self.public_egress_enabled = public_egress_enabled
        self._task_store = task_store
        self._context_store = context_store
        self._checkpoint_store = checkpoint_store
        self._gateway_identity_verifier = gateway_identity_verifier
        self._external_transport = external_transport
        self._control_plane = control_plane
        self._hosted_http_client = hosted_http_client
        self._dispatcher = A2ATaskEventDispatcher(
            event_outbox,
            control_plane,
            retry_interval_seconds=outbox_retry_interval_seconds,
        )
        self._mounted = False
        self._server: Any = None

    @classmethod
    def from_platform(cls, **kwargs: Any) -> "AgentEngineA2ABootstrap":
        """Explicit product factory; dependencies are injected by the platform layer."""

        return cls(**kwargs)

    @property
    def event_dispatcher(self) -> A2ATaskEventDispatcher:
        return self._dispatcher

    @property
    def server(self) -> Any:
        return self._server

    def client_for_space(self, space_id: str):
        """Create a Space-scoped client sharing Runtime identity, clients, and outbox."""

        from ksadk.a2a.space_client import A2ASpaceClient

        return A2ASpaceClient(
            space_id,
            self._control_plane,
            egress_enabled=self.public_egress_enabled,
            httpx_client=self._hosted_http_client,
            external_transport=self._external_transport,
            event_dispatcher=self._dispatcher,
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
            expected_target_agent_id=self.runtime_metadata.agent_id,
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
        )
        self._server = add_a2a_protocol_routes(
            app,
            runner,
            config,
            task_adapter=task_adapter,
            task_store=_require(self._task_store, "task_store"),
            context_builder=A2AOwnerContextBuilder(
                identity_resolver=A2ATrustedIdentityResolver(),
                allow_unverified_identity=False,
            ),
        )
        return self._server

    async def start(self) -> None:
        if self._context_store is not None:
            await _initialize_if_supported(self._context_store)
        if self._checkpoint_store is not None:
            await _initialize_if_supported(self._checkpoint_store)
        await self._dispatcher.start()

    async def stop(self) -> None:
        await self._dispatcher.stop()


async def _initialize_if_supported(value: Any) -> None:
    initialize = getattr(value, "initialize", None)
    if not callable(initialize):
        return
    result = initialize()
    if inspect.isawaitable(result):
        await result


def _require(value: Any, name: str) -> Any:
    if value is None:
        raise RuntimeError(f"AgentEngineA2ABootstrap is missing {name}")
    return value


__all__ = ["AgentEngineA2ABootstrap", "RuntimeA2AMetadata"]
