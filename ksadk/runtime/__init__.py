"""Side-effect-free public exports for the canonical RuntimeAdapter stack."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ADKRuntimeAdapter": ("ksadk.runtime.framework_adapters", "ADKRuntimeAdapter"),
    "BaseRuntime": ("ksadk.runtime.adapter", "BaseRuntime"),
    "CancelResult": ("ksadk.runtime.adapter", "CancelResult"),
    "PauseResult": ("ksadk.runtime.adapter", "PauseResult"),
    "CheckpointCapability": ("ksadk.runtime.adapter", "CheckpointCapability"),
    "CheckpointDescriptor": ("ksadk.runtime.adapter", "CheckpointDescriptor"),
    "ConversationPreprocessingRequest": (
        "ksadk.runtime.adapter",
        "ConversationPreprocessingRequest",
    ),
    "CONVERSATION_PREPROCESSING_METADATA_KEY": (
        "ksadk.runtime.adapter",
        "CONVERSATION_PREPROCESSING_METADATA_KEY",
    ),
    "LangGraphRuntimeAdapter": (
        "ksadk.runtime.framework_adapters",
        "LangGraphRuntimeAdapter",
    ),
    "RESUME_START_REQUEST_NATIVE_KEY": (
        "ksadk.runtime.adapter",
        "RESUME_START_REQUEST_NATIVE_KEY",
    ),
    "ResumePayload": ("ksadk.runtime.adapter", "ResumePayload"),
    "ResumeTarget": ("ksadk.runtime.adapter", "ResumeTarget"),
    "RunHandle": ("ksadk.runtime.adapter", "RunHandle"),
    "RunnerRuntimeAdapter": ("ksadk.runtime.runner_adapter", "RunnerRuntimeAdapter"),
    "RuntimeAdapter": ("ksadk.runtime.adapter", "RuntimeAdapter"),
    "RuntimeAdapterFactory": ("ksadk.runtime.adapter", "RuntimeAdapterFactory"),
    "RuntimeExecutor": ("ksadk.runtime.executor", "RuntimeExecutor"),
    "RuntimeStartPreparation": (
        "ksadk.runtime.executor",
        "RuntimeStartPreparation",
    ),
    "RuntimeLaunchContext": ("ksadk.runtime.adapter", "RuntimeLaunchContext"),
    "RuntimeRegistry": ("ksadk.runtime.adapter", "RuntimeRegistry"),
    "RuntimeServices": ("ksadk.runtime.adapter", "RuntimeServices"),
    "StartRequest": ("ksadk.runtime.adapter", "StartRequest"),
    "build_default_runtime_registry": (
        "ksadk.runtime.factory",
        "build_default_runtime_registry",
    ),
    "create_runtime_adapter": ("ksadk.runtime.factory", "create_runtime_adapter"),
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
