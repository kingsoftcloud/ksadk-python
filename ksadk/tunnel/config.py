"""Environment and composition helpers for the optional MCP Tunnel."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlparse

_TRUE_VALUES = {"1", "true", "yes", "on"}
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class TunnelConfig:
    """Validated process-local Tunnel settings."""

    agent_name: str
    token: str = field(repr=False)
    self_url: str


def tunnel_enabled() -> bool:
    return os.getenv("KSADK_ENABLE_TUNNEL", "").strip().lower() in _TRUE_VALUES


def default_tunnel_self_url(port: int | str | None = None) -> str:
    """Resolve the loopback URL used by the MCP toolset to reach its own app."""
    explicit = os.getenv("KSADK_TUNNEL_SELF_URL", "").strip()
    if explicit:
        return _validate_self_url(explicit)
    selected = port if port is not None else os.getenv("PORT", "8000")
    try:
        port_number = int(selected)
    except (TypeError, ValueError) as exc:
        raise ValueError("Tunnel self port must be an integer") from exc
    if not 1 <= port_number <= 65535:
        raise ValueError("Tunnel self port must be between 1 and 65535")
    return f"http://127.0.0.1:{port_number}"


def tunnel_self_url_for_start(port: int | str | None = None) -> str | None:
    """Resolve a startup URL only when Tunnel is enabled."""
    return default_tunnel_self_url(port) if tunnel_enabled() else None


def tunnel_config_for_detection(
    detection: Any,
    *,
    runtime_type: str | None = None,
    self_url: str | None = None,
) -> TunnelConfig | None:
    """Build a validated config, returning ``None`` when Tunnel is disabled."""
    if not tunnel_enabled():
        return None

    detection_type = getattr(getattr(detection, "type", None), "value", None)
    selected_runtime = str(runtime_type or detection_type or "").strip().lower()
    supported_types = {"adk", "langgraph"}
    if selected_runtime not in supported_types or (
        detection_type and detection_type not in supported_types
    ):
        raise ValueError(
            "KSADK_ENABLE_TUNNEL currently supports only ADK and custom LangGraph "
            "StateGraph runtimes"
        )

    agent_name = str(getattr(detection, "name", "") or "").strip()
    if not agent_name:
        raise ValueError("Tunnel requires a non-empty detected Agent name")

    token = os.getenv("KSADK_TUNNEL_TOKEN", "").strip()
    if not token:
        raise ValueError("KSADK_TUNNEL_TOKEN is required when Tunnel is enabled")

    return TunnelConfig(
        agent_name=agent_name,
        token=token,
        self_url=_validate_self_url(self_url) if self_url else default_tunnel_self_url(),
    )


def tunnel_config_from_context(
    context: Any,
    *,
    self_url: str | None = None,
) -> TunnelConfig | None:
    registry = getattr(context.services, "tunnel_registry", None)
    resolved_self_url = self_url or getattr(registry, "self_url", None)
    return tunnel_config_for_detection(
        context.detection,
        runtime_type=context.runtime_type,
        self_url=resolved_self_url,
    )


def prepare_tunnel_services(
    services: Any,
    *,
    runtime_type: str,
    detection: Any,
    self_url: str | None = None,
) -> Any:
    """Create or refine the app-owned registry without replacing its identity."""
    registry = getattr(services, "tunnel_registry", None)
    config = tunnel_config_for_detection(
        detection,
        runtime_type=runtime_type,
        self_url=self_url or getattr(registry, "self_url", None),
    )
    if config is None:
        return services

    if registry is None:
        from ksadk.tunnel.registry import TunnelRegistry

        registry = TunnelRegistry()
    registry.self_url = config.self_url
    if getattr(services, "tunnel_registry", None) is registry:
        return services
    return replace(services, tunnel_registry=registry)


def prepare_tunnel_context(
    context: Any,
    *,
    self_url: str | None = None,
) -> tuple[Any, TunnelConfig | None]:
    """Prepare one launch context and return its effective Tunnel config."""
    config = tunnel_config_from_context(context, self_url=self_url)
    if config is None:
        return context, None

    services = prepare_tunnel_services(
        context.services,
        runtime_type=context.runtime_type,
        detection=context.detection,
        self_url=config.self_url,
    )
    if services is not context.services:
        context = replace(context, services=services)
        config = tunnel_config_from_context(context)
    return context, config


def _validate_self_url(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("KSADK_TUNNEL_SELF_URL must be an absolute HTTP(S) URL")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("KSADK_TUNNEL_SELF_URL must not contain credentials, query, or fragment")
    if (parsed.hostname or "").lower() not in _LOOPBACK_HOSTS:
        raise ValueError("KSADK_TUNNEL_SELF_URL must point to the loopback host")
    return candidate


__all__ = [
    "TunnelConfig",
    "default_tunnel_self_url",
    "prepare_tunnel_context",
    "prepare_tunnel_services",
    "tunnel_config_for_detection",
    "tunnel_config_from_context",
    "tunnel_enabled",
    "tunnel_self_url_for_start",
]
