"""Public Provider index without eagerly starting optional runtimes.

The catalog must remain usable in a base install.  ADK-backed Harness code is
loaded only when a caller actually asks for a Harness-specific symbol.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "BUILTIN_PROVIDER_VERSION": ("legacy_catalog", "BUILTIN_PROVIDER_VERSION"),
    "CODEX_AGENT_PROVIDER_PLUGIN_ID": ("legacy_catalog", "CODEX_AGENT_PROVIDER_PLUGIN_ID"),
    "KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID": (
        "legacy_catalog",
        "KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID",
    ),
    "builtin_agent_provider_manifests": ("legacy_catalog", "builtin_agent_provider_manifests"),
    "legacy_harness_agent_provider_manifest": (
        "legacy_catalog",
        "legacy_harness_agent_provider_manifest",
    ),
    "CodexAgentProviderFactory": ("codex", "CodexAgentProviderFactory"),
    "CodexAgentProviderRuntime": ("codex", "CodexAgentProviderRuntime"),
    "CodexProviderInventory": ("codex", "CodexProviderInventory"),
    "CodexTurnRequest": ("codex", "CodexTurnRequest"),
    "CodexTurnResult": ("codex", "CodexTurnResult"),
    "SHIPPED_CODEX_DSH_PACKAGE": ("codex_dsh", "SHIPPED_CODEX_DSH_PACKAGE"),
    "SHIPPED_CODEX_PROVIDER_ID": ("codex_dsh", "SHIPPED_CODEX_PROVIDER_ID"),
    "SHIPPED_CODEX_PROVIDER_VERSION": ("codex_dsh", "SHIPPED_CODEX_PROVIDER_VERSION"),
    "KsADKCodexDshBridgeFactory": ("codex_dsh", "KsADKCodexDshBridgeFactory"),
    "KsADKCodexDshBridgeRuntime": ("codex_dsh", "KsADKCodexDshBridgeRuntime"),
    "ShippedCodexDshBundle": ("codex_dsh", "ShippedCodexDshBundle"),
    "shipped_codex_dsh_bundle": ("codex_dsh", "shipped_codex_dsh_bundle"),
    "shipped_codex_dsh_host_command": ("codex_dsh", "shipped_codex_dsh_host_command"),
    "DSH_AGENT_PROVIDER_HOST_METHODS": ("dsh", "DSH_AGENT_PROVIDER_HOST_METHODS"),
    "DSH_AGENT_PROVIDER_HOST_PROTOCOL": ("dsh", "DSH_AGENT_PROVIDER_HOST_PROTOCOL"),
    "DSH_HOST_USER_PERMISSION": ("dsh", "DSH_HOST_USER_PERMISSION"),
    "DshAgentProviderDescriptor": ("dsh", "DshAgentProviderDescriptor"),
    "DshAgentProviderFactory": ("dsh", "DshAgentProviderFactory"),
    "DshAgentProviderHost": ("dsh", "DshAgentProviderHost"),
    "DshAgentProviderInventory": ("dsh", "DshAgentProviderInventory"),
    "DshAgentProviderPreflight": ("dsh", "DshAgentProviderPreflight"),
    "DshAgentProviderRegistration": ("dsh", "DshAgentProviderRegistration"),
    "DshAgentProviderRuntime": ("dsh", "DshAgentProviderRuntime"),
    "DshPreparedAgent": ("dsh", "DshPreparedAgent"),
    "dsh_agent_provider_manifest": ("dsh", "dsh_agent_provider_manifest"),
    "HarnessContextSource": ("harness", "HarnessContextSource"),
    "HarnessMCPSource": ("harness", "HarnessMCPSource"),
    "HarnessProviderInventory": ("harness", "HarnessProviderInventory"),
    "HarnessSkillContribution": ("harness", "HarnessSkillContribution"),
    "HarnessSkillSource": ("harness", "HarnessSkillSource"),
    "HarnessTurnRequest": ("harness", "HarnessTurnRequest"),
    "HarnessTurnResult": ("harness", "HarnessTurnResult"),
    "KsADKHarnessProviderFactory": ("harness", "KsADKHarnessProviderFactory"),
    "KsADKHarnessProviderRuntime": ("harness", "KsADKHarnessProviderRuntime"),
    "SHIPPED_HARNESS_DSH_PACKAGE": ("harness_dsh", "SHIPPED_HARNESS_DSH_PACKAGE"),
    "KsADKHarnessDshBridgeFactory": ("harness_dsh", "KsADKHarnessDshBridgeFactory"),
    "KsADKHarnessDshBridgeRuntime": ("harness_dsh", "KsADKHarnessDshBridgeRuntime"),
    "ShippedHarnessDshBundle": ("harness_dsh", "ShippedHarnessDshBundle"),
    "shipped_harness_dsh_bundle": ("harness_dsh", "shipped_harness_dsh_bundle"),
    "shipped_harness_dsh_host_command": ("harness_dsh", "shipped_harness_dsh_host_command"),
    "HarnessProviderSelection": ("legacy", "HarnessProviderSelection"),
    "LegacyBundleAdapter": ("legacy", "LegacyBundleAdapter"),
    "LegacyBundleCompatibilityError": ("legacy", "LegacyBundleCompatibilityError"),
    "LegacyHarnessSource": ("legacy", "LegacyHarnessSource"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        relative_module, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    module = import_module(f"{__name__}.{relative_module}")
    value = getattr(module, attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
