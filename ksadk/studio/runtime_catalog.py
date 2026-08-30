"""RuntimeAdapter installation facts exposed to Studio authoring."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

from ksadk.managed_runtime import installed_runtime_version
from ksadk.runtime import RuntimeExecutor

_RUNTIMES = {
    "codex": {
        "package": "openai-codex",
        "displayName": "Codex",
        "adapter": "CodexRuntimeAdapter",
        "capabilities": {
            "session": True,
            "cancel": True,
            "resume": "thread_id",
            "checkpoint": "thread",
        },
    },
    "adk": {
        "package": "google-adk",
        "displayName": "Google ADK",
        "adapter": "ADKRuntimeAdapter",
        "capabilities": {
            "session": True,
            "cancel": True,
            "resume": "forward_only",
            "checkpoint": "runtime_dependent",
        },
    },
    "langgraph": {
        "package": "langgraph",
        "displayName": "LangGraph",
        "adapter": "LangGraphRuntimeAdapter",
        "capabilities": {
            "session": True,
            "cancel": True,
            "resume": "checkpoint_id",
            "checkpoint": "runtime_dependent",
        },
    },
}


def inspect_runtime_catalog(executor: RuntimeExecutor) -> list[dict]:
    items: list[dict] = []
    for runtime_type in executor.registered_runtime_types():
        metadata = _RUNTIMES[runtime_type]
        package = str(metadata["package"])
        try:
            installed = (
                installed_runtime_version("codex")
                if runtime_type == "codex"
                else package_version(package)
            )
        except PackageNotFoundError:
            installed = ""
        items.append(
            {
                "runtimeType": runtime_type,
                **metadata,
                "installed": bool(installed),
                "version": installed or None,
                "status": "ready" if installed else "missing-dependency",
                "installCommand": f"pip install 'ksadk[{runtime_type}]'",
            }
        )
    return items


__all__ = ["inspect_runtime_catalog"]
