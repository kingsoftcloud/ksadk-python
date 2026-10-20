"""Serializable launch configuration and explicit E2B owner cleanup callbacks."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ksadk.studio.native_background_connection import BackgroundHostDeclaration


def e2b_source_config(
    backend: Any, declaration: BackgroundHostDeclaration | None = None,
) -> dict[str, Any]:
    """Build the serializable supervisor config from an ``E2BSandboxBackend``.

    When a host declaration is present, its non-sensitive typed
    ``connection_target`` is carried alongside the credential-bearing
    ``connection`` so the supervisor freezes the canonical authority into
    sandbox.json at launch. The key stays in ``connection`` (private, over the
    stdin pipe only) and is never written to sandbox.json.
    """
    spec = backend.spec
    connection = backend.connection
    config: dict[str, Any] = {
        "source_kind": "e2b",
        "spec": {
            "template_id": spec.template_id,
            "sandbox_type": spec.sandbox_type.value,
            "timeout": spec.timeout,
            "allow_internet_access": spec.allow_internet_access,
            "metadata": dict(spec.metadata),
            "env": dict(spec.env),
        },
        "connection": (
            {
                "api_url": connection.api_url,
                "domain": connection.domain,
                "api_key": connection.api_key.get_secret_value(),
                "traffic_source": connection.traffic_source,
            }
            if connection is not None
            else None
        ),
    }
    if declaration is not None:
        config["connection_target"] = declaration.target.to_dict()
    return config


def _e2b_kill_sandbox(backend: Any) -> Callable[[str], bool]:
    def kill(sandbox_id: str) -> bool:
        session = backend.reconnect_session(session_locator=sandbox_id)
        return bool(session.kill())

    return kill
