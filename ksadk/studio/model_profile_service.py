"""Model Profile connection diagnostics."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

from ksadk.studio.contracts import ModelSpec, NetworkPolicy
from ksadk.studio.errors import StudioError


async def test_model_profile_connection(
    *,
    catalog: Any,
    model_client: Any,
    resource_id: str,
) -> dict:
    descriptor = catalog.get(resource_id)
    if descriptor.kind != "model":
        raise StudioError(
            "RESOURCE_KIND_INVALID",
            "连接测试只能用于 Model Profile",
            status_code=422,
            details={"resourceId": resource_id},
        )
    spec = ModelSpec.model_validate(descriptor.contract)
    resolved = catalog.resolver.resolve_model(spec)
    resolved.parameters = resolved.parameters.model_copy(
        update={
            "temperature": 0,
            "max_tokens": min(resolved.parameters.max_tokens, 16),
        }
    )
    host = (urlparse(resolved.endpoint_url).hostname or "").lower().rstrip(".")
    started = time.monotonic()
    response = await model_client.complete(
        resolved,
        messages=[{"role": "user", "content": "这是连接测试。请只回复 OK。"}],
        network_policy=NetworkPolicy(
            mode="restricted",
            allowed_hosts=[host] if host else [],
            allow_private_network=False,
        ),
        timeout_seconds=20,
        max_attempts=1,
        backoff_seconds=0,
    )
    return {
        "ok": True,
        "resourceId": resource_id,
        "model": resolved.model,
        "finishReason": response.finish_reason,
        "latencyMs": int((time.monotonic() - started) * 1000),
    }


__all__ = ["test_model_profile_connection"]
