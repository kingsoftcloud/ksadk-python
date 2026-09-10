"""RuntimeAdapter installation facts exposed to Studio authoring."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

from ksadk.harness.managed_runtime import ManagedHarnessRuntime
from ksadk.managed_runtime import installed_runtime_version
from ksadk.runtime import RuntimeExecutor

_RUNTIMES = {
    "harness": {
        "package": "ksadk",
        "displayName": "KsADK Harness",
        "adapter": "ManagedHarnessRuntimeAdapter",
    },
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


def _harness_capabilities() -> dict:
    """从 DSH Provider 实际装配的 Managed Harness Runtime 读取真实能力声明。

    进程内 resume/checkpoint 为既有实现；跨进程持久性取决于装配档位
    （Workspace 状态目录 / Checkpoint DSN），目录如实标注当前声明。
    """

    # StudioPluginRuntime always supplies a Workspace state directory to the
    # shipped DSH Harness provider. The catalog describes that product path,
    # not a separately constructed in-memory SDK adapter.
    native = ManagedHarnessRuntime(durable=True).native_capabilities()
    checkpoint = native.get("checkpoint") or {}
    session_continuity = native.get("session_continuity") or {}
    return {
        "session": True,
        "cancel": bool((native.get("cancel") or {}).get("supported")),
        "resume": (
            "checkpoint_id" if session_continuity.get("durable")
            else "in_process" if native.get("resume", {}).get("supported")
            else "not_supported"
        ),
        "checkpoint": (
            "workspace" if session_continuity.get("durable")
            else "in_process" if checkpoint.get("supported")
            else "not_supported"
        ),
        "checkpointGranularity": checkpoint.get("granularity"),
        "durableAcrossProcess": bool(session_continuity.get("durable")),
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
                else package_version("ksadk")
                if runtime_type == "harness"
                else package_version(package)
            )
        except PackageNotFoundError:
            installed = ""
        entry = {
            "runtimeType": runtime_type,
            **metadata,
            "installed": bool(installed),
            "version": installed or None,
            "status": "ready" if installed else "missing-dependency",
            "installCommand": (
                "pip install ksadk" if runtime_type == "harness"
                else f"pip install 'ksadk[{runtime_type}]'"
            ),
        }
        if runtime_type == "harness":
            entry["capabilities"] = _harness_capabilities()
        items.append(entry)
    return items


__all__ = ["inspect_runtime_catalog"]
