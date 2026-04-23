"""CLI storage helpers for serverless PVC defaults."""

from __future__ import annotations

from typing import Optional

import click


DEFAULT_STORAGE_SIZE_GI = 20
MIN_STORAGE_SIZE_GI = 20
MAX_STORAGE_SIZE_GI = 500
SERVERLESS_TARGETS = {"serverless", "kcf", "kce"}

_DEFAULT_MOUNT_PATHS = {
    "adk": "/home/node/.agentengine",
    "langchain": "/home/node/.agentengine",
    "langgraph": "/home/node/.agentengine",
    "deepagents": "/home/node/.agentengine",
    "hermes": "/home/node/.hermes",
    "openclaw": "/home/node/.openclaw",
}


def resolve_default_storage_mount_path(framework: str) -> str:
    normalized = str(framework or "").strip().lower()
    return _DEFAULT_MOUNT_PATHS.get(normalized, "/home/node/.agentengine")


def validate_storage_size_gi(value: Optional[int]) -> int:
    size_gi = DEFAULT_STORAGE_SIZE_GI if value is None else int(value)
    if size_gi < MIN_STORAGE_SIZE_GI or size_gi > MAX_STORAGE_SIZE_GI:
        raise click.BadParameter(
            f"挂盘容量必须在 {MIN_STORAGE_SIZE_GI}Gi 到 {MAX_STORAGE_SIZE_GI}Gi 之间"
        )
    return size_gi


def validate_storage_mount_path(value: Optional[str]) -> str:
    path = str(value or "").strip()
    if not path:
        raise click.BadParameter("挂盘目录不能为空")
    if not path.startswith("/") or path == "/":
        raise click.BadParameter("挂盘目录必须是绝对路径，且不能为 /")
    return path.rstrip("/") or path


def should_enable_storage(*, target: Optional[str], no_storage: bool) -> bool:
    return not no_storage and str(target or "").strip().lower() in SERVERLESS_TARGETS


def build_storage_config(
    framework: str,
    *,
    target: Optional[str] = None,
    no_storage: bool = False,
    mount_path: Optional[str] = None,
    size_gi: Optional[int] = None,
):
    if target is not None and not should_enable_storage(target=target, no_storage=no_storage):
        return None
    if no_storage:
        return None

    resolved_mount_path = (
        validate_storage_mount_path(mount_path)
        if mount_path is not None
        else resolve_default_storage_mount_path(framework)
    )
    return {
        "mount_path": resolved_mount_path,
        "size_gi": validate_storage_size_gi(size_gi),
    }
