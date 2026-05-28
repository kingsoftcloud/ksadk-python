"""Workspace file management commands."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path, PurePosixPath
from typing import Callable

import click

from ksadk.api import AgentEngineAPIError, AgentEngineClient
from ksadk.cli.agent_ref import merge_agent_inputs, resolve_agent_ref, resolve_openclaw_ref
from ksadk.cli.error_utils import ensure_dry_run_supported
from ksadk.cli.resource_common import CONTEXT_SETTINGS
from ksadk.cli.ui import output_option as cli_output_option
from ksadk.deployment.state import load_state

DEFAULT_REGION = "cn-beijing-6"
DEFAULT_WORKSPACE_ROOT_LABEL = "workspace"
LOCAL_DEV_ARTIFACT_DIR_NAMES = frozenset(
    {
        ".agentengine",
        ".claude",
        ".codex",
        ".git",
        ".idea",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        ".vscode",
        "__pycache__",
        "node_modules",
        "venv",
    }
)
LOCAL_DEV_ARTIFACT_FILE_NAMES = frozenset(
    {
        ".DS_Store",
        ".agentengine.state",
        ".coverage",
        "Thumbs.db",
    }
)
LOCAL_DEV_ARTIFACT_FILE_SUFFIXES = (".pyc", ".pyo", ".swp", ".tmp")


def _format_size(size_bytes: object | None) -> str:
    if size_bytes is None:
        return "-"
    try:
        value = int(size_bytes)
    except (TypeError, ValueError):
        return str(size_bytes)
    if value < 1024:
        return f"{value} B"
    units = ["KB", "MB", "GB", "TB"]
    scaled = float(value)
    for unit in units:
        scaled /= 1024
        if scaled < 1024 or unit == units[-1]:
            text = f"{scaled:.1f}".rstrip("0").rstrip(".")
            return f"{text} {unit}"
    return f"{value} B"


def _workspace_root_label(payload: dict | None = None) -> str:
    candidate = ""
    if isinstance(payload, dict):
        candidate = str(payload.get("workspace_root") or payload.get("root") or "").strip()
    return candidate or DEFAULT_WORKSPACE_ROOT_LABEL


def _workspace_display_path(path: object | None, *, root_label: str | None = None) -> str:
    root = str(root_label or DEFAULT_WORKSPACE_ROOT_LABEL).strip() or DEFAULT_WORKSPACE_ROOT_LABEL
    raw = str(path or ".").strip().replace("\\", "/")
    if raw in {"", ".", "/"}:
        return f"{root}:/"
    normalized = "/".join(part for part in raw.split("/") if part not in {"", "."})
    return f"{root}:/{normalized}"


def _workspace_real_path(path: object | None, *, workspace_root: str | None) -> str | None:
    root = str(workspace_root or "").strip().replace("\\", "/")
    if not root:
        return None
    if root != "/":
        root = root.rstrip("/")
    raw = str(path or ".").strip().replace("\\", "/")
    if raw in {"", ".", "/"}:
        return root
    normalized = "/".join(part for part in raw.lstrip("/").split("/") if part not in {"", "."})
    if not normalized:
        return root
    if root == "/":
        return f"/{normalized}"
    return f"{root}/{normalized}"


def _echo_key_value(label: str, value: str, *, fg: str = "bright_blue", bold: bool = False) -> None:
    click.secho(f"{label}：{value}", fg=fg, bold=bold)


def _echo_section_title(title: str, *, fg: str = "bright_blue") -> None:
    click.secho(title, fg=fg, bold=True)


def _normalize_entry_payload(entry: dict, *, root_label: str, workspace_real_root: str | None = None) -> dict:
    normalized = dict(entry or {})
    path = str(normalized.get("path") or "").strip()
    normalized["display_path"] = _workspace_display_path(path, root_label=root_label)
    normalized["real_path"] = _workspace_real_path(path, workspace_root=workspace_real_root)
    size_bytes = normalized.get("size_bytes")
    if size_bytes is not None:
        normalized["size_human"] = _format_size(size_bytes)
    else:
        normalized["size_human"] = None
    return normalized


def _build_list_payload(payload: dict) -> dict:
    root_label = _workspace_root_label(payload)
    workspace_real_root = str(payload.get("workspace_real_root") or payload.get("workspace_path") or "").strip() or None
    entries = [
        _normalize_entry_payload(
            entry,
            root_label=root_label,
            workspace_real_root=workspace_real_root,
        )
        for entry in payload.get("entries", [])
    ]
    directories = [entry for entry in entries if entry.get("type") == "directory"]
    files = [entry for entry in entries if entry.get("type") != "directory"]
    enriched = dict(payload)
    enriched.update(
        {
            "ok": True,
            "action": "list",
            "workspace_root": root_label,
            "workspace_display_path": _workspace_display_path(payload.get("path", "."), root_label=root_label),
            "workspace_real_root": workspace_real_root,
            "workspace_real_path": _workspace_real_path(
                payload.get("path", "."),
                workspace_root=workspace_real_root,
            ),
            "entry_count": len(entries),
            "directories": directories,
            "files": files,
            "entries": entries,
            "summary": {
                "entry_count": len(entries),
                "directory_count": len(directories),
                "file_count": len(files),
            },
        }
    )
    return enriched


def _build_upload_payload(payload: dict, *, local_path: Path, remote_path: str) -> dict:
    root_label = _workspace_root_label(payload)
    entry = payload.get("entry", {}) if isinstance(payload.get("entry"), dict) else {}
    resolved_remote_path = str(entry.get("path") or remote_path)
    normalized_entry = _normalize_entry_payload(
        {
            **entry,
            "path": resolved_remote_path,
        },
        root_label=root_label,
    )
    enriched = dict(payload)
    enriched.update(
        {
            "ok": True,
            "action": "upload",
            "workspace_root": root_label,
            "local_path": str(local_path),
            "requested_remote_path": remote_path,
            "remote_path": resolved_remote_path,
            "remote_display_path": _workspace_display_path(resolved_remote_path, root_label=root_label),
            "entry": normalized_entry,
            "summary": {
                "uploaded": 1,
                "size_bytes": normalized_entry.get("size_bytes"),
                "size_human": normalized_entry.get("size_human"),
            },
        }
    )
    return enriched


def _build_download_payload(*, remote_path: str, output_path: Path, size_bytes: int) -> dict:
    root_label = DEFAULT_WORKSPACE_ROOT_LABEL
    return {
        "ok": True,
        "action": "download",
        "workspace_root": root_label,
        "remote_path": remote_path,
        "remote_display_path": _workspace_display_path(remote_path, root_label=root_label),
        "output_path": str(output_path),
        "local_path": str(output_path),
        "size_bytes": size_bytes,
        "size_human": _format_size(size_bytes),
        "summary": {
            "downloaded": 1,
            "size_bytes": size_bytes,
            "size_human": _format_size(size_bytes),
        },
    }


def _build_delete_payload(payload: dict, *, remote_path: str) -> dict:
    root_label = _workspace_root_label(payload)
    enriched = dict(payload)
    enriched.update(
        {
            "ok": True,
            "action": "delete",
            "workspace_root": root_label,
            "remote_path": remote_path,
            "remote_display_path": _workspace_display_path(remote_path, root_label=root_label),
            "summary": {
                "deleted": 1 if payload.get("deleted") else 0,
            },
        }
    )
    return enriched


def _build_sync_results(payload: dict) -> dict:
    direction = str(payload.get("direction", "")).strip().lower()
    root_label = _workspace_root_label(payload)
    local_dir = Path(str(payload.get("local_dir", ".")))
    results: dict[str, list[dict]] = {}
    for key in ("created", "overwritten", "skipped"):
        items: list[dict] = []
        for path in payload.get(key, []):
            raw_path = str(path)
            if direction == "push":
                display_path = _workspace_display_path(raw_path, root_label=root_label)
            else:
                display_path = str((local_dir / Path(raw_path)).resolve())
            items.append(
                {
                    "path": raw_path,
                    "display_path": display_path,
                }
            )
        results[key] = items
    return results


def _build_sync_payload(payload: dict) -> dict:
    root_label = _workspace_root_label(payload)
    transport_mode = str(payload.get("transport_mode") or "").strip().lower()
    transport_hint = {
        "action_proxy": "通过平台 action 代理访问远端 workspace",
        "runtime_direct": "通过 runtime endpoint 直连远端 workspace",
    }.get(transport_mode)
    enriched = dict(payload)
    enriched.update(
        {
            "ok": True,
            "action": str(payload.get("direction", "sync")).strip().lower() or "sync",
            "workspace_root": root_label,
            "remote_display_path": _workspace_display_path(payload.get("remote_path", "."), root_label=root_label),
            "transport_mode": transport_mode or None,
            "transport_hint": transport_hint,
            "summary": {
                "created_count": len(payload.get("created", [])),
                "overwritten_count": len(payload.get("overwritten", [])),
                "skipped_count": len(payload.get("skipped", [])),
                "total_files": payload.get("total_files", 0),
            },
            "results": _build_sync_results(payload),
        }
    )
    return enriched


def _emit_payload(payload, output_mode: str | None) -> None:
    if output_mode == "json":
        click.echo(json.dumps(payload, ensure_ascii=False))
        return
    if isinstance(payload, dict) and "entries" in payload:
        root_label = _workspace_root_label(payload)
        entries = payload.get("entries", [])
        _echo_key_value("工作空间", root_label, fg="bright_blue", bold=True)
        _echo_key_value(
            "当前目录",
            _workspace_display_path(payload.get("path", "."), root_label=root_label),
            fg="cyan",
            bold=True,
        )
        workspace_real_path = str(payload.get("workspace_real_path") or "").strip()
        if workspace_real_path:
            _echo_key_value("实际目录", workspace_real_path, fg="magenta")
        _echo_key_value("条目数量", str(len(entries)), fg="yellow")
        if not entries:
            click.secho("当前目录为空。", fg="bright_black")
            return
        directories = payload.get("directories") or [entry for entry in entries if entry.get("type") == "directory"]
        files = payload.get("files") or [entry for entry in entries if entry.get("type") != "directory"]
        if directories:
            _echo_section_title(f"目录（{len(directories)}）", fg="cyan")
            for entry in directories:
                click.secho(f"  {entry.get('display_path') or _workspace_display_path(entry.get('path', ''), root_label=root_label)}", fg="cyan")
        if files:
            _echo_section_title(f"文件（{len(files)}）", fg="green")
            for entry in files:
                click.secho(
                    f"  {entry.get('display_path') or _workspace_display_path(entry.get('path', ''), root_label=root_label)}"
                    f"  {entry.get('size_human') or _format_size(entry.get('size_bytes'))}",
                    fg="green",
                )
        return
    click.echo(str(payload))


def _emit_sync_payload(payload: dict, output_mode: str | None) -> None:
    if output_mode == "json":
        click.echo(json.dumps(payload, ensure_ascii=False))
        return

    root_label = _workspace_root_label(payload)
    direction = str(payload.get("direction", "sync")).strip().lower()
    title = {
        "push": "推送完成",
        "pull": "拉取完成",
    }.get(direction, "同步完成")
    _echo_section_title(title, fg="green")
    _echo_key_value("本地目录", str(payload.get("local_dir", ".")), fg="bright_blue")
    _echo_key_value(
        "远端目录",
        _workspace_display_path(payload.get("remote_path", "."), root_label=root_label),
        fg="cyan",
    )
    transport_hint = str(payload.get("transport_hint") or "").strip()
    if transport_hint:
        _echo_key_value("访问链路", transport_hint, fg="magenta")
    click.secho(
        "统计："
        f"新增 {len(payload.get('created', []))}，"
        f"覆盖 {len(payload.get('overwritten', []))}，"
        f"跳过 {len(payload.get('skipped', []))}，"
        f"共 {payload.get('total_files', 0)}",
        fg="yellow",
    )
    for key, label in (
        ("created", "已新增"),
        ("overwritten", "已覆盖"),
        ("skipped", "已跳过"),
    ):
        for item in payload.get(key, []):
            color = {
                "created": "green",
                "overwritten": "yellow",
                "skipped": "bright_black",
            }.get(key, "white")
            click.secho(
                f"{label}：{_workspace_display_path(item, root_label=root_label)}",
                fg=color,
            )


def _normalize(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_workspace_dir(value: str | None) -> str:
    raw = str(value or ".").strip().replace("\\", "/")
    if raw in {"", ".", "/"}:
        return "."
    parts = [part for part in PurePosixPath(raw.lstrip("/")).parts if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise click.UsageError("workspace 目录路径不能包含 ..")
    return "." if not parts else "/".join(parts)


def _normalize_workspace_file_path(value: str | None) -> str:
    normalized = _normalize_workspace_dir(value)
    if normalized == ".":
        raise click.UsageError("workspace 文件路径不能为空或根目录")
    return normalized


def _relative_remote_path(entry_path: str, base_remote_path: str) -> str:
    normalized_entry = _normalize_workspace_dir(entry_path)
    if base_remote_path == ".":
        return normalized_entry
    prefix = f"{base_remote_path}/"
    if normalized_entry.startswith(prefix):
        return normalized_entry[len(prefix) :]
    return normalized_entry


def _join_remote_path(base_remote_path: str, relative_path: str) -> str:
    relative = str(relative_path).strip().replace("\\", "/")
    if base_remote_path == ".":
        return relative
    return f"{base_remote_path}/{relative}"


def _ignored_local_dev_artifact_label_for_file(filename: str) -> str | None:
    if filename in LOCAL_DEV_ARTIFACT_FILE_NAMES:
        return filename
    for suffix in LOCAL_DEV_ARTIFACT_FILE_SUFFIXES:
        if filename.endswith(suffix):
            return f"*{suffix}"
    return None


def _collect_local_files_report(
    local_dir: Path,
    *,
    ignore_dev_artifacts: bool = False,
    ignore_git_artifacts: bool = True,
) -> dict:
    files: list[tuple[str, Path]] = []
    ignored_artifacts: set[str] = set()
    total_bytes = 0
    ignored_dir_names = set(LOCAL_DEV_ARTIFACT_DIR_NAMES)
    if not ignore_git_artifacts:
        ignored_dir_names.discard(".git")

    for root, dirnames, filenames in os.walk(local_dir, topdown=True):
        root_path = Path(root)
        if ignore_dev_artifacts:
            kept_dirs: list[str] = []
            for dirname in sorted(dirnames):
                if dirname in ignored_dir_names:
                    ignored_artifacts.add(dirname)
                    continue
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs
        else:
            dirnames[:] = sorted(dirnames)

        for filename in sorted(filenames):
            if ignore_dev_artifacts:
                ignored_label = _ignored_local_dev_artifact_label_for_file(filename)
                if ignored_label:
                    ignored_artifacts.add(ignored_label)
                    continue
            file_path = root_path / filename
            relative = file_path.relative_to(local_dir).as_posix()
            files.append((relative, file_path))
            total_bytes += int(file_path.stat().st_size)

    return {
        "files": files,
        "total_files": len(files),
        "total_bytes": total_bytes,
        "ignored_artifacts": sorted(ignored_artifacts),
    }


def _collect_local_files(
    local_dir: Path,
    *,
    ignore_dev_artifacts: bool = False,
    ignore_git_artifacts: bool = True,
) -> list[tuple[str, Path]]:
    return _collect_local_files_report(
        local_dir,
        ignore_dev_artifacts=ignore_dev_artifacts,
        ignore_git_artifacts=ignore_git_artifacts,
    )["files"]


def _workspace_client_kwargs(
    *,
    agent_ref: str | None,
    endpoint: str | None,
    api_key: str | None,
) -> dict:
    kwargs = {"agent_id": agent_ref}
    if endpoint:
        kwargs["endpoint"] = endpoint
    if api_key:
        kwargs["api_key"] = api_key
    return kwargs


def _state_matches_target(state: dict, target_agent: str | None) -> bool:
    if not state or not target_agent:
        return False
    return target_agent == state.get("agent_id") or target_agent == state.get("name")


def _state_prefers_action_proxy(state: dict) -> bool:
    if not isinstance(state, dict):
        return False
    framework = str(state.get("framework") or "").strip().lower()
    state_type = str(state.get("type") or "").strip().lower()
    return framework == "openclaw" or state_type == "openclaw"


def _resolve_workspace_region(region: str | None, state: dict) -> str:
    return _normalize(region) or _normalize(state.get("region")) or os.getenv("KSYUN_REGION") or DEFAULT_REGION


def _resolve_workspace_agent_ref(explicit_ref: str | None, cwd: Path) -> str | None:
    if explicit_ref:
        return explicit_ref

    state_ref = resolve_agent_ref(
        None,
        cwd=cwd,
        include_state=True,
        include_project_config=False,
    )
    if state_ref:
        return state_ref.value

    openclaw_state_ref = resolve_openclaw_ref(
        None,
        cwd=cwd,
        include_state=True,
    )
    if openclaw_state_ref:
        return openclaw_state_ref.value

    config_ref = resolve_agent_ref(
        None,
        cwd=cwd,
        include_state=False,
        include_project_config=True,
    )
    if config_ref:
        return config_ref.value

    return None


def _resolve_workspace_runtime_access(
    *,
    state: dict,
    target_agent: str | None,
    endpoint: str | None,
    api_key: str | None,
) -> tuple[str | None, str | None]:
    endpoint_value = _normalize(endpoint)
    api_key_value = _normalize(api_key)
    if endpoint_value:
        return endpoint_value, api_key_value
    if not _state_matches_target(state, target_agent):
        return None, api_key_value
    state_endpoint = _normalize(state.get("endpoint"))
    state_api_key = api_key_value or _normalize(state.get("api_key"))
    if _state_prefers_action_proxy(state):
        if state_endpoint and state_api_key:
            return state_endpoint, state_api_key
        return None, api_key_value
    return state_endpoint, state_api_key


def _resolve_workspace_command_context(
    *,
    agent_option: str | None,
    positional_agent: str | None,
    endpoint: str | None,
    api_key: str | None,
    region: str | None,
) -> tuple[str | None, str, str | None, str | None]:
    try:
        agent_input = merge_agent_inputs(
            agent_option=agent_option,
            positional_agent=positional_agent,
        )
    except ValueError as e:
        raise click.UsageError(str(e)) from e

    cwd = Path(".").resolve()
    state = load_state(cwd)
    resolved_region = _resolve_workspace_region(region, state)
    target_agent = _resolve_workspace_agent_ref(agent_input, cwd)
    resolved_endpoint, resolved_api_key = _resolve_workspace_runtime_access(
        state=state,
        target_agent=target_agent,
        endpoint=endpoint,
        api_key=api_key,
    )
    if target_agent or resolved_endpoint:
        return target_agent, resolved_region, resolved_endpoint, resolved_api_key

    raise click.UsageError(
        "请指定 Agent（--agent 或位置参数）、--endpoint，或在当前目录提供可解析的本地配置\n"
        "自动解析顺序: .agentengine.state -> agentengine.yaml/ksadk.yaml"
    )


async def _list_workspace_files(
    *,
    agent_ref: str | None,
    path: str,
    recursive: bool,
    region: str,
    endpoint: str | None,
    api_key: str | None,
):
    async with AgentEngineClient(region=region) as client:
        kwargs = {
            "agent_id": agent_ref,
            "path": path,
            "recursive": recursive,
        }
        if endpoint:
            kwargs["endpoint"] = endpoint
        if api_key:
            kwargs["api_key"] = api_key
        payload = await client.list_workspace_files(**kwargs)
        try:
            health = await client.get_workspace_health(
                agent_id=agent_ref,
                endpoint=endpoint,
                api_key=api_key,
            )
        except Exception:
            health = {}
        if health:
            payload = dict(payload)
            if health.get("root") and not payload.get("root"):
                payload["root"] = health["root"]
            if health.get("workspace_path"):
                payload["workspace_real_root"] = health["workspace_path"]
        return payload


async def _upload_workspace_file(
    *,
    agent_ref: str | None,
    remote_path: str,
    local_path: Path,
    region: str,
    endpoint: str | None,
    api_key: str | None,
):
    async with AgentEngineClient(region=region) as client:
        kwargs = {
            "agent_id": agent_ref,
            "remote_path": remote_path,
            "local_path": local_path,
        }
        if endpoint:
            kwargs["endpoint"] = endpoint
        if api_key:
            kwargs["api_key"] = api_key
        return await client.upload_workspace_file(**kwargs)


async def _download_workspace_file(
    *,
    agent_ref: str | None,
    remote_path: str,
    region: str,
    endpoint: str | None,
    api_key: str | None,
) -> bytes:
    async with AgentEngineClient(region=region) as client:
        kwargs = {
            "agent_id": agent_ref,
            "remote_path": remote_path,
        }
        if endpoint:
            kwargs["endpoint"] = endpoint
        if api_key:
            kwargs["api_key"] = api_key
        return await client.download_workspace_file(**kwargs)


async def _delete_workspace_file(
    *,
    agent_ref: str | None,
    remote_path: str,
    region: str,
    endpoint: str | None,
    api_key: str | None,
):
    async with AgentEngineClient(region=region) as client:
        kwargs = {
            "agent_id": agent_ref,
            "remote_path": remote_path,
        }
        if endpoint:
            kwargs["endpoint"] = endpoint
        if api_key:
            kwargs["api_key"] = api_key
        return await client.delete_workspace_file(**kwargs)


async def _push_workspace_files(
    *,
    agent_ref: str | None,
    local_dir: Path,
    remote_path: str,
    force: bool,
    region: str,
    endpoint: str | None,
    api_key: str | None,
    progress_callback: Callable[[dict], None] | None = None,
    ignore_dev_artifacts: bool = False,
    ignore_git_artifacts: bool = True,
) -> dict:
    remote_dir = _normalize_workspace_dir(remote_path)
    local_report = _collect_local_files_report(
        local_dir,
        ignore_dev_artifacts=ignore_dev_artifacts,
        ignore_git_artifacts=ignore_git_artifacts,
    )
    local_files = local_report["files"]
    total_bytes = int(local_report["total_bytes"])
    remote_existing: set[str] = set()
    transport_mode = ""
    client_kwargs = _workspace_client_kwargs(
        agent_ref=agent_ref,
        endpoint=endpoint,
        api_key=api_key,
    )

    async with AgentEngineClient(region=region) as client:
        if progress_callback:
            progress_callback(
                {
                    "phase": "list_start",
                    "remote_path": remote_dir,
                    "total": len(local_files),
                }
            )
        try:
            payload = await client.list_workspace_files(
                path=remote_dir,
                recursive=True,
                **client_kwargs,
            )
        except AgentEngineAPIError as exc:
            if exc.code != 404:
                raise
            payload = {"entries": []}
        transport_mode = str(payload.get("transport_mode") or "").strip()
        if progress_callback:
            progress_callback(
                {
                    "phase": "list_done",
                    "remote_path": remote_dir,
                    "total": len(local_files),
                    "remote_entry_count": len(payload.get("entries", [])),
                }
            )

        for entry in payload.get("entries", []):
            if entry.get("type") == "file" and entry.get("path"):
                remote_existing.add(str(entry["path"]))

        created: list[str] = []
        overwritten: list[str] = []
        skipped: list[str] = []
        total_files = len(local_files)
        for index, (relative_path, absolute_path) in enumerate(local_files, start=1):
            destination_path = _join_remote_path(remote_dir, relative_path)
            if destination_path in remote_existing and not force:
                skipped.append(destination_path)
                if progress_callback:
                    progress_callback(
                        {
                            "phase": "upload_skipped",
                            "current": index,
                            "total": total_files,
                            "remote_path": destination_path,
                            "local_path": str(absolute_path),
                            "size_bytes": int(absolute_path.stat().st_size),
                            "total_bytes": total_bytes,
                        }
                    )
                continue
            if progress_callback:
                progress_callback(
                    {
                        "phase": "upload_start",
                        "current": index,
                        "total": total_files,
                        "remote_path": destination_path,
                        "local_path": str(absolute_path),
                        "size_bytes": int(absolute_path.stat().st_size),
                        "total_bytes": total_bytes,
                    }
                )
            response = await client.upload_workspace_file(
                remote_path=destination_path,
                local_path=absolute_path,
                **client_kwargs,
            )
            if not transport_mode:
                transport_mode = str(response.get("transport_mode") or "").strip()
            resolved_path = response.get("entry", {}).get("path", destination_path)
            if destination_path in remote_existing:
                overwritten.append(resolved_path)
            else:
                created.append(resolved_path)
            if progress_callback:
                progress_callback(
                    {
                        "phase": "upload_done",
                        "current": index,
                        "total": total_files,
                        "remote_path": resolved_path,
                        "local_path": str(absolute_path),
                        "size_bytes": int(absolute_path.stat().st_size),
                        "total_bytes": total_bytes,
                    }
                )

    return {
        "direction": "push",
        "local_dir": str(local_dir),
        "remote_path": remote_dir,
        "workspace_root": DEFAULT_WORKSPACE_ROOT_LABEL,
        "transport_mode": transport_mode or None,
        "force": bool(force),
        "total_files": len(local_files),
        "created": created,
        "overwritten": overwritten,
        "skipped": skipped,
    }


async def _pull_workspace_files(
    *,
    agent_ref: str | None,
    local_dir: Path,
    remote_path: str,
    force: bool,
    region: str,
    endpoint: str | None,
    api_key: str | None,
) -> dict:
    remote_dir = _normalize_workspace_dir(remote_path)
    transport_mode = ""
    client_kwargs = _workspace_client_kwargs(
        agent_ref=agent_ref,
        endpoint=endpoint,
        api_key=api_key,
    )

    async with AgentEngineClient(region=region) as client:
        payload = await client.list_workspace_files(
            path=remote_dir,
            recursive=True,
            **client_kwargs,
        )
        transport_mode = str(payload.get("transport_mode") or "").strip()
        remote_files = sorted(
            [
                entry
                for entry in payload.get("entries", [])
                if entry.get("type") == "file" and entry.get("path")
            ],
            key=lambda entry: str(entry.get("path", "")),
        )

        created: list[str] = []
        overwritten: list[str] = []
        skipped: list[str] = []
        for entry in remote_files:
            remote_file_path = str(entry["path"])
            relative_path = _relative_remote_path(remote_file_path, remote_dir)
            target_path = local_dir / Path(relative_path)
            existed_before = target_path.exists()
            if existed_before and target_path.is_dir():
                raise click.ClickException(f"本地目标 {target_path} 是目录，无法写入文件")
            if existed_before and not force:
                skipped.append(relative_path)
                continue

            content = await client.download_workspace_file(
                remote_path=remote_file_path,
                **client_kwargs,
            )
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(content)
            if existed_before:
                overwritten.append(relative_path)
            else:
                created.append(relative_path)

    return {
        "direction": "pull",
        "local_dir": str(local_dir),
        "remote_path": remote_dir,
        "workspace_root": DEFAULT_WORKSPACE_ROOT_LABEL,
        "transport_mode": transport_mode or None,
        "force": bool(force),
        "total_files": len(remote_files),
        "created": created,
        "overwritten": overwritten,
        "skipped": skipped,
    }


@click.group("files", context_settings=CONTEXT_SETTINGS)
def files():
    """管理 Agent workspace 文件。"""


@files.command("list", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@click.option("--agent", "agent_option", "-a", help="Agent ID")
@click.option("--endpoint", "-e", help="Agent Endpoint URL (覆盖自动获取)")
@click.option("--api-key", help="Runtime API Key (与 --endpoint 搭配使用)")
@click.option("--path", default=".", show_default=True, help="Workspace 目录路径")
@click.option("--recursive", is_flag=True, help="递归列出目录")
@click.option("--region", "-r", default=None, envvar=None, help="区域 (默认优先读取 .agentengine.state)")
@cli_output_option()
def list_files(
    agent_ref: str | None,
    agent_option: str | None,
    endpoint: str | None,
    api_key: str | None,
    path: str,
    recursive: bool,
    region: str | None,
    output_mode: str | None,
):
    """列出 workspace 文件。"""
    ensure_dry_run_supported("agentengine files list")
    normalized_path = _normalize_workspace_dir(path)
    agent_ref, region, endpoint, api_key = _resolve_workspace_command_context(
        agent_option=agent_option,
        positional_agent=agent_ref,
        endpoint=endpoint,
        api_key=api_key,
        region=region,
    )
    payload = asyncio.run(
        _list_workspace_files(
            agent_ref=agent_ref,
            path=normalized_path,
            recursive=recursive,
            region=region,
            endpoint=endpoint,
            api_key=api_key,
        )
    )
    normalized_payload = _build_list_payload(payload)
    _emit_payload(normalized_payload, output_mode)


@files.command("upload", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@click.option("--agent", "agent_option", "-a", help="Agent ID")
@click.option("--endpoint", "-e", help="Agent Endpoint URL (覆盖自动获取)")
@click.option("--api-key", help="Runtime API Key (与 --endpoint 搭配使用)")
@click.option(
    "--local-path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="本地文件路径",
)
@click.option("--remote-path", required=True, help="Workspace 目标路径")
@click.option("--region", "-r", default=None, envvar=None, help="区域 (默认优先读取 .agentengine.state)")
@cli_output_option()
def upload_file(
    agent_ref: str | None,
    agent_option: str | None,
    endpoint: str | None,
    api_key: str | None,
    local_path: Path,
    remote_path: str,
    region: str | None,
    output_mode: str | None,
):
    """上传文件到 workspace。"""
    ensure_dry_run_supported("agentengine files upload")
    normalized_remote_path = _normalize_workspace_file_path(remote_path)
    agent_ref, region, endpoint, api_key = _resolve_workspace_command_context(
        agent_option=agent_option,
        positional_agent=agent_ref,
        endpoint=endpoint,
        api_key=api_key,
        region=region,
    )
    payload = asyncio.run(
        _upload_workspace_file(
            agent_ref=agent_ref,
            remote_path=normalized_remote_path,
            local_path=local_path,
            region=region,
            endpoint=endpoint,
            api_key=api_key,
        )
    )
    normalized_payload = _build_upload_payload(
        payload,
        local_path=local_path,
        remote_path=normalized_remote_path,
    )
    if output_mode == "json":
        click.echo(json.dumps(normalized_payload, ensure_ascii=False))
        return
    entry = normalized_payload.get("entry", {})
    _echo_section_title("上传完成", fg="green")
    _echo_key_value("本地文件", normalized_payload["local_path"], fg="bright_blue")
    _echo_key_value(
        "远端文件",
        normalized_payload["remote_display_path"],
        fg="cyan",
    )
    if entry.get("size_bytes") is not None:
        _echo_key_value("文件大小", normalized_payload["summary"]["size_human"], fg="yellow")


@files.command("download", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@click.option("--agent", "agent_option", "-a", help="Agent ID")
@click.option("--endpoint", "-e", help="Agent Endpoint URL (覆盖自动获取)")
@click.option("--api-key", help="Runtime API Key (与 --endpoint 搭配使用)")
@click.option("--remote-path", required=True, help="Workspace 文件路径")
@click.option(
    "--output-path",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="本地输出文件路径",
)
@click.option("--region", "-r", default=None, envvar=None, help="区域 (默认优先读取 .agentengine.state)")
@cli_output_option()
def download_file(
    agent_ref: str | None,
    agent_option: str | None,
    endpoint: str | None,
    api_key: str | None,
    remote_path: str,
    output_path: Path,
    region: str | None,
    output_mode: str | None,
):
    """下载 workspace 文件。"""
    ensure_dry_run_supported("agentengine files download")
    normalized_remote_path = _normalize_workspace_file_path(remote_path)
    agent_ref, region, endpoint, api_key = _resolve_workspace_command_context(
        agent_option=agent_option,
        positional_agent=agent_ref,
        endpoint=endpoint,
        api_key=api_key,
        region=region,
    )
    content = asyncio.run(
        _download_workspace_file(
            agent_ref=agent_ref,
            remote_path=normalized_remote_path,
            region=region,
            endpoint=endpoint,
            api_key=api_key,
        )
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(content)
    payload = _build_download_payload(
        remote_path=normalized_remote_path,
        output_path=output_path,
        size_bytes=len(content),
    )
    if output_mode == "json":
        click.echo(json.dumps(payload, ensure_ascii=False))
        return
    _echo_section_title("下载完成", fg="green")
    _echo_key_value("远端文件", payload["remote_display_path"], fg="cyan")
    _echo_key_value("本地文件", payload["local_path"], fg="bright_blue")
    _echo_key_value("文件大小", payload["size_human"], fg="yellow")


@files.command("delete", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@click.option("--agent", "agent_option", "-a", help="Agent ID")
@click.option("--endpoint", "-e", help="Agent Endpoint URL (覆盖自动获取)")
@click.option("--api-key", help="Runtime API Key (与 --endpoint 搭配使用)")
@click.option("--remote-path", required=True, help="Workspace 文件路径")
@click.option("--yes", is_flag=True, help="跳过确认")
@click.option("--region", "-r", default=None, envvar=None, help="区域 (默认优先读取 .agentengine.state)")
@cli_output_option()
def delete_file(
    agent_ref: str | None,
    agent_option: str | None,
    endpoint: str | None,
    api_key: str | None,
    remote_path: str,
    yes: bool,
    region: str | None,
    output_mode: str | None,
):
    """删除 workspace 文件。"""
    ensure_dry_run_supported("agentengine files delete")
    normalized_remote_path = _normalize_workspace_file_path(remote_path)
    agent_ref, region, endpoint, api_key = _resolve_workspace_command_context(
        agent_option=agent_option,
        positional_agent=agent_ref,
        endpoint=endpoint,
        api_key=api_key,
        region=region,
    )
    if not yes and not click.confirm(f"删除 workspace 文件 {normalized_remote_path}?", default=False):
        raise click.Abort()
    payload = asyncio.run(
        _delete_workspace_file(
            agent_ref=agent_ref,
            remote_path=normalized_remote_path,
            region=region,
            endpoint=endpoint,
            api_key=api_key,
        )
    )
    normalized_payload = _build_delete_payload(payload, remote_path=normalized_remote_path)
    if output_mode == "json":
        click.echo(json.dumps(normalized_payload, ensure_ascii=False))
        return
    _echo_section_title("删除完成", fg="red")
    _echo_key_value("远端文件", normalized_payload["remote_display_path"], fg="cyan")


@files.command("push", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@click.option("--agent", "agent_option", "-a", help="Agent ID")
@click.option("--endpoint", "-e", help="Agent Endpoint URL (覆盖自动获取)")
@click.option("--api-key", help="Runtime API Key (与 --endpoint 搭配使用)")
@click.option(
    "--local-dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    required=True,
    help="本地目录路径",
)
@click.option("--remote-path", default=".", show_default=True, help="Workspace 目标目录")
@click.option("--force", is_flag=True, help="覆盖远端已存在的同名文件")
@click.option("--region", "-r", default=None, envvar=None, help="区域 (默认优先读取 .agentengine.state)")
@cli_output_option()
def push_files(
    agent_ref: str | None,
    agent_option: str | None,
    endpoint: str | None,
    api_key: str | None,
    local_dir: Path,
    remote_path: str,
    force: bool,
    region: str | None,
    output_mode: str | None,
):
    """递归推送本地目录到 workspace 子目录。"""
    ensure_dry_run_supported("agentengine files push")
    normalized_remote_path = _normalize_workspace_dir(remote_path)
    agent_ref, region, endpoint, api_key = _resolve_workspace_command_context(
        agent_option=agent_option,
        positional_agent=agent_ref,
        endpoint=endpoint,
        api_key=api_key,
        region=region,
    )
    payload = asyncio.run(
        _push_workspace_files(
            agent_ref=agent_ref,
            local_dir=local_dir,
            remote_path=normalized_remote_path,
            force=force,
            region=region,
            endpoint=endpoint,
            api_key=api_key,
        )
    )
    normalized_payload = _build_sync_payload(payload)
    _emit_sync_payload(normalized_payload, output_mode)


@files.command("pull", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@click.option("--agent", "agent_option", "-a", help="Agent ID")
@click.option("--endpoint", "-e", help="Agent Endpoint URL (覆盖自动获取)")
@click.option("--api-key", help="Runtime API Key (与 --endpoint 搭配使用)")
@click.option("--remote-path", default=".", show_default=True, help="Workspace 源目录")
@click.option(
    "--local-dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    required=True,
    help="本地输出目录",
)
@click.option("--force", is_flag=True, help="覆盖本地已存在的同名文件")
@click.option("--region", "-r", default=None, envvar=None, help="区域 (默认优先读取 .agentengine.state)")
@cli_output_option()
def pull_files(
    agent_ref: str | None,
    agent_option: str | None,
    endpoint: str | None,
    api_key: str | None,
    remote_path: str,
    local_dir: Path,
    force: bool,
    region: str | None,
    output_mode: str | None,
):
    """递归拉取 workspace 子目录到本地目录。"""
    ensure_dry_run_supported("agentengine files pull")
    normalized_remote_path = _normalize_workspace_dir(remote_path)
    agent_ref, region, endpoint, api_key = _resolve_workspace_command_context(
        agent_option=agent_option,
        positional_agent=agent_ref,
        endpoint=endpoint,
        api_key=api_key,
        region=region,
    )
    local_dir.mkdir(parents=True, exist_ok=True)
    payload = asyncio.run(
        _pull_workspace_files(
            agent_ref=agent_ref,
            local_dir=local_dir,
            remote_path=normalized_remote_path,
            force=force,
            region=region,
            endpoint=endpoint,
            api_key=api_key,
        )
    )
    normalized_payload = _build_sync_payload(payload)
    _emit_sync_payload(normalized_payload, output_mode)
