"""Deployment-owned tunnel configuration and app/runner binding."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .registry import TunnelRegistry
from .server import TunnelAgentPolicy


@dataclass(frozen=True)
class TunnelBinding:
    policy: TunnelAgentPolicy
    registry: TunnelRegistry

    async def start(self):
        if self.registry.peers is not None:
            await self.registry.peers.start()

    async def close(self):
        await self.registry.close()


class DeferredTunnel:
    """Resolve the exported declaration on normal runner load or the first connector request."""

    def __init__(self, launch):
        from threading import Lock

        self.launch = launch
        self.binding = None
        self.resolved = False
        self._lock = Lock()
        self.peers = None

    def _peer_registry(self):
        # Peer requests use only the resolved registry; they never import agents.
        return self.binding.registry if self.binding is not None else None

    async def start(self):
        if self.peers is not None:
            await self.peers.start()

    def resolve(self, root, module):
        from ksadk.agent import TunnelDeclaration

        with self._lock:
            if not self.resolved:
                declaration = getattr(root, "_ksadk_tunnel_declaration", None)
                if isinstance(declaration, TunnelDeclaration) and declaration.enabled:
                    entry = self.launch.detection.entry_point
                    if (
                        Path(module.__file__).resolve()
                        != (self.launch.project_dir / entry).resolve()
                    ):
                        raise ValueError("Tunnel declaration loaded from a different project")
                    token = os.environ.get(declaration.token_env, "")
                    if not token.strip():
                        raise ValueError(
                            f"Missing tunnel token in environment variable {declaration.token_env}"
                        )
                    peers = _peer_network(self._peer_registry, token=token)
                    self.binding = TunnelBinding(
                        TunnelAgentPolicy("root", token, services=None), TunnelRegistry()
                    )
                    self.peers = peers
                    self.binding.registry.peers = self.peers
                self.resolved = True
            return self.binding

    def for_connector(self):
        if not self.resolved:
            from ksadk.runners.utils import load_agent_module

            detection = self.launch.detection
            root, module = load_agent_module(
                str(self.launch.project_dir), detection.entry_point, detection.agent_variable
            )
            return self.resolve(root, module)
        return self.binding

    async def close(self):
        if self.binding is not None:
            await self.binding.registry.close()
        elif self.peers is not None:
            await self.peers.close()


def bind_runner(runner, binding: TunnelBinding) -> None:
    """Bind before lazy runner initialization; reject cross-app registry reuse."""
    if isinstance(binding, DeferredTunnel):
        runner._deferred_tunnel = binding
        return
    current = getattr(runner, "_tunnel_binding", None)
    if current is not None and current != binding:
        raise ValueError("runner already belongs to another tunnel binding")
    if current is None and getattr(runner, "_agent", None) is not None:
        raise ValueError("MCP tunnel must be configured before runner initialization")
    runner._tunnel_binding = binding


def configure_app_tunnel(config):
    """Prepare per-app tunnel state without importing the agent."""
    launch = config.launch_context
    tunnel_binding = launch.services.tunnel_binding if launch else None
    if (
        launch
        and launch.runtime_type in {"adk", "langgraph"}
        and getattr(launch.detection, "entry_point", None)
        and getattr(launch.detection, "agent_variable", None)
        and tunnel_binding is None
    ):
        tunnel_binding = DeferredTunnel(launch)
    if tunnel_binding is not None:
        from dataclasses import replace

        if launch.runtime_type not in {"adk", "langgraph"}:
            raise ValueError("Tool tunnel requires an ADK or LangGraph RuntimeLaunchContext")
        # Keep the caller's reusable config/context untouched: every app gets
        # its own registry unless an explicit process-owned binding is supplied.
        from copy import copy

        config = copy(config)
        if not isinstance(tunnel_binding, DeferredTunnel) and tunnel_binding.registry.peers is None:
            tunnel_binding.registry.peers = _peer_network(
                lambda: tunnel_binding.registry, token=tunnel_binding.policy.token
            )
        config.launch_context = replace(
            launch, services=replace(launch.services, tunnel_binding=tunnel_binding)
        )
        if config.a2a_runtime_adapter is not None:
            configure_tunnel = getattr(config.a2a_runtime_adapter, "configure_tunnel", None)
            if callable(configure_tunnel):
                configure_tunnel(tunnel_binding)
            elif not isinstance(tunnel_binding, DeferredTunnel):
                raise ValueError("A2A adapter must support the app's tool tunnel binding")

    return config, tunnel_binding


def _peer_network(registry_provider, *, token):
    from .peers import PeerConfig, PeerNetwork

    config = PeerConfig.from_env(tunnel_token=token)
    return PeerNetwork(config, registry_provider) if config is not None else None
