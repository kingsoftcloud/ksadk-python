"""Workspace file management commands."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import click

from ksadk.api import AgentEngineClient
from ksadk.cli.agent_ref import merge_agent_inputs, resolve_agent_ref, resolve_openclaw_ref
from ksadk.cli.error_utils import ensure_dry_run_supported
from ksadk.cli.resource_common import CONTEXT_SETTINGS
from ksadk.cli.ui import output_option as cli_output_option
from ksadk.deployment.state import load_state

DEFAULT_REGION = "cn-beijing-6"


def _emit_payload(payload, output_mode: str | None) -> None:
    if output_mode == "json":
        click.echo(json.dumps(payload, ensure_ascii=False))
        return
    if isinstance(payload, dict) and "entries" in payload:
        click.echo(f"Workspace: {payload.get('root', 'workspace')} @ {payload.get('path', '.')}")
        for entry in payload.get("entries", []):
            prefix = "[DIR]" if entry.get("type") == "directory" else "[FILE]"
            suffix = f" ({entry['size_bytes']} bytes)" if entry.get("size_bytes") is not None else ""
            click.echo(f"{prefix} {entry.get('path', '')}{suffix}")
        return
    click.echo(str(payload))


def _normalize(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _state_matches_target(state: dict, target_agent: str | None) -> bool:
    if not state or not target_agent:
        return False
    return target_agent == state.get("agent_id") or target_agent == state.get("name")


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
    return _normalize(state.get("endpoint")), api_key_value or _normalize(state.get("api_key"))


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
        return await client.list_workspace_files(**kwargs)


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
            path=path,
            recursive=recursive,
            region=region,
            endpoint=endpoint,
            api_key=api_key,
        )
    )
    _emit_payload(payload, output_mode)


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
            remote_path=remote_path,
            local_path=local_path,
            region=region,
            endpoint=endpoint,
            api_key=api_key,
        )
    )
    if output_mode == "json":
        click.echo(json.dumps(payload, ensure_ascii=False))
        return
    click.echo(f"Uploaded: {payload.get('entry', {}).get('path', remote_path)}")


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
            remote_path=remote_path,
            region=region,
            endpoint=endpoint,
            api_key=api_key,
        )
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(content)
    payload = {"remote_path": remote_path, "output_path": str(output_path), "size_bytes": len(content)}
    if output_mode == "json":
        click.echo(json.dumps(payload, ensure_ascii=False))
        return
    click.echo(f"Downloaded: {remote_path} -> {output_path}")


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
    agent_ref, region, endpoint, api_key = _resolve_workspace_command_context(
        agent_option=agent_option,
        positional_agent=agent_ref,
        endpoint=endpoint,
        api_key=api_key,
        region=region,
    )
    if not yes and not click.confirm(f"删除 workspace 文件 {remote_path}?", default=False):
        raise click.Abort()
    payload = asyncio.run(
        _delete_workspace_file(
            agent_ref=agent_ref,
            remote_path=remote_path,
            region=region,
            endpoint=endpoint,
            api_key=api_key,
        )
    )
    if output_mode == "json":
        click.echo(json.dumps(payload, ensure_ascii=False))
        return
    click.echo(f"Deleted: {remote_path}")
