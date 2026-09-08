"""Explicit compatibility catalog for pre-DSH AgentProvider manifests.

All new AgentProviders, including the official Codex and KsADK Harness
Bundles, enter Bundle v2 composition only through a ready DSH registration.
The old in-process Harness manifest remains an explicit compatibility artifact
for already-built Agents; callers must never add it to new resolution silently.
"""

from __future__ import annotations

import hashlib

from ksadk.plugins.contracts import PluginManifest

BUILTIN_PROVIDER_VERSION = "1.0.0"
CODEX_AGENT_PROVIDER_PLUGIN_ID = "io.ksadk.codex-provider"
KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID = "io.ksadk.harness-provider"


def _digest(plugin_id: str) -> str:
    payload = f"{plugin_id}@{BUILTIN_PROVIDER_VERSION}:builtin-provider-v1".encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _provider_manifest(
    plugin_id: str,
    *,
    domain: str,
    runtime: str,
    isolation: str,
    source: str,
    entrypoint: str | None = None,
) -> PluginManifest:
    spec: dict[str, object] = {
        "domain": domain,
        "runtime": runtime,
        "provides": [
            {
                "definition": "agent.provider/v1",
                "slot": "agent.execution",
                "mode": "unique",
            }
        ],
        "permissions": [],
        "isolation": isolation,
        "compatibility": {
            "kernelApi": ">=1,<2",
            "runtimeProtocols": ["agentkit.runtime/v1"],
            "python": ">=3.10,<3.15" if runtime == "python" else None,
        },
        "healthContract": "plugin.health/v1",
        "provenance": {
            "source": source,
            "digest": _digest(plugin_id),
            "license": "Apache-2.0",
        },
    }
    if entrypoint is not None:
        spec["entrypoint"] = entrypoint
    compatibility = spec["compatibility"]
    assert isinstance(compatibility, dict)
    if compatibility["python"] is None:
        del compatibility["python"]
    manifest = PluginManifest.model_validate(
        {
            "metadata": {"id": plugin_id, "version": BUILTIN_PROVIDER_VERSION},
            "spec": spec,
        }
    )
    if not isinstance(manifest, PluginManifest):  # pragma: no cover - Pydantic invariant
        raise TypeError("built-in provider manifest validation returned an invalid type")
    return manifest


def builtin_agent_provider_manifests() -> tuple[PluginManifest, ...]:
    """Return no Bundle v2 providers; DSH registration is authoritative."""

    return ()


def legacy_harness_agent_provider_manifest() -> PluginManifest:
    """Return the old manifest only for an explicitly detected legacy Agent."""

    return _provider_manifest(
        KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID,
        domain="ksadk-platform",
        runtime="python",
        isolation="in-process",
        source="builtin",
        entrypoint="ksadk.plugins.providers.harness:KsADKHarnessProviderFactory",
    )


__all__ = [
    "BUILTIN_PROVIDER_VERSION",
    "CODEX_AGENT_PROVIDER_PLUGIN_ID",
    "KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID",
    "builtin_agent_provider_manifests",
    "legacy_harness_agent_provider_manifest",
]
