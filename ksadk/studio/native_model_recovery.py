"""Explicit host grant for the narrow ordinary Studio Chat Completions path.

Route/type checks prove local construction identity, never provider behavior.
The caller separately attests absence of provider-hosted side effects. This
private prototype refuses custom clients, metadata and cross-boot recovery.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field

from ksadk.harness.native.model_takeover import ModelTakeoverCapability
from ksadk.harness.native.state import Contract


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def same_boot_clock_domain() -> str:
    if sys.platform == "darwin":
        boot = subprocess.check_output(
            ["/usr/sbin/sysctl", "-n", "kern.boottime"], text=True, timeout=5
        ).strip()
        if not boot.startswith("{ sec = "):
            raise ValueError("boot clock domain unavailable")
    elif sys.platform.startswith("linux"):
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if len(boot) != 36:
            raise ValueError("boot clock domain unavailable")
    else:
        raise ValueError("same-boot takeover clock unsupported")
    return _digest({"version": "same-boot-monotonic/v1", "platform": sys.platform, "boot": boot})


def route_identity(endpoint: str, model: str) -> str:
    return _digest({"endpoint": endpoint, "model": model})


class StudioModelTakeoverDeclaration(Contract):
    version: Literal["studio-pure-model-host/v1"] = "studio-pure-model-host/v1"
    endpoint_model_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Host assertions, NOT conclusions inferred from the route or class name.
    provider_side_effects_absent: Literal[True]
    hard_aggregate_token_or_cost_budget: Literal[False]
    clock_domain_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def digest(self):
        return _digest(self.model_dump(mode="json"))


def admit_studio_port(port, declaration: StudioModelTakeoverDeclaration) -> None:
    from ksadk.model_gateway.client import ModelGatewayClient
    from ksadk.studio.native_model_port import StudioNativeModelPort

    if type(port) is not StudioNativeModelPort or type(port._client) is not ModelGatewayClient:
        raise ValueError("model takeover client construction unsupported")
    model = port._model
    if (
        model.metadata
        or model.discovery
        or model.wire_api not in {None, "chat"}
        or not urlparse(model.endpoint_url).path.endswith("/chat/completions")
        or port._client.transport is not None
    ):
        raise ValueError("model takeover request construction unsupported")
    if route_identity(model.endpoint_url, model.model) != declaration.endpoint_model_digest:
        raise ValueError("model takeover route identity changed")
    if declaration.clock_domain_digest != same_boot_clock_domain():
        raise ValueError("model takeover clock domain changed")
    port._model_takeover_declaration = declaration


def assert_local_function_proposals(options) -> None:
    allowed = {
        "messages",
        "network_policy",
        "compatibility_profile",
        "timeout_seconds",
        "tools",
        "max_output_tokens",
        "output_budget_mode",
        "output_token_parameter",
        "reasoning_effort",
        "tool_choice",
        "parallel_tool_calls",
    }
    if set(options) - allowed:
        raise ValueError("model takeover request options unknown")
    for tool in options.get("tools", []):
        if (
            not isinstance(tool, dict)
            or set(tool) != {"type", "function"}
            or tool["type"] != "function"
        ):
            raise ValueError("model takeover requires local function proposals")


def bound_capability(port, declaration, host_binding_digest):
    admit_studio_port(port, declaration)
    return ModelTakeoverCapability(
        host_binding_digest=host_binding_digest,
        model_port_identity_digest=_digest(
            {
                "port": "StudioNativeModelPort/v1",
                "route": declaration.endpoint_model_digest,
                "profile": port._profile_digest,
                "request": "local-function-proposals-only/v1",
            }
        ),
        provider_side_effects_absent=declaration.provider_side_effects_absent,
        tool_calls_are_local_proposals=True,
        hard_aggregate_token_or_cost_budget=declaration.hard_aggregate_token_or_cost_budget,
        clock_domain_digest=declaration.clock_domain_digest,
    )
