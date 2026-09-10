"""Public Harness surface without eagerly requiring an ADK runtime.

Studio must be usable for Codex and DSH Providers from a base ``ksadk``
installation. Harness keeps ADK as an optional execution dependency, so
importing its configuration types cannot import the full Harness runtime.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "HarnessApp": ("ksadk.harness.app", "HarnessApp"),
    "HarnessCapabilities": ("ksadk.harness.app", "HarnessCapabilities"),
    "HarnessPlugin": ("ksadk.harness.app", "HarnessPlugin"),
    "HarnessConfig": ("ksadk.harness.config", "HarnessConfig"),
    "HarnessConfigError": ("ksadk.harness.config", "HarnessConfigError"),
    "McpToolSpec": ("ksadk.harness.config", "McpToolSpec"),
    "SandboxPolicy": ("ksadk.harness.config", "SandboxPolicy"),
    "HarnessReasoner": ("ksadk.harness.reasoner", "HarnessReasoner"),
    "HarnessReasoningTurn": ("ksadk.harness.reasoner", "HarnessReasoningTurn"),
    "HarnessToolCall": ("ksadk.harness.reasoner", "HarnessToolCall"),
    "LiteLLMHarnessReasoner": ("ksadk.harness.reasoner", "LiteLLMHarnessReasoner"),
    "AcceptanceCheck": ("ksadk.harness.run_control", "AcceptanceCheck"),
    "MilestoneSpec": ("ksadk.harness.run_control", "MilestoneSpec"),
    "RunControlSpec": ("ksadk.harness.run_control", "RunControlSpec"),
    "RunLimits": ("ksadk.harness.run_control", "RunLimits"),
    "StagnationPolicy": ("ksadk.harness.run_control", "StagnationPolicy"),
    "HarnessRuntime": ("ksadk.harness.runtime", "HarnessRuntime"),
    "HarnessRuntimeAdapter": ("ksadk.harness.runtime", "HarnessRuntimeAdapter"),
    "HarnessSandboxExecutor": ("ksadk.harness.sandbox", "HarnessSandboxExecutor"),
    "SandboxPolicyDenied": ("ksadk.harness.sandbox", "SandboxPolicyDenied"),
    "ToolDeliverySemantics": (
        "ksadk.harness.tool_reliability",
        "ToolDeliverySemantics",
    ),
    "ToolReliability": ("ksadk.harness.tool_reliability", "ToolReliability"),
    "classify_tool_reliability": (
        "ksadk.harness.tool_reliability",
        "classify_tool_reliability",
    ),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
