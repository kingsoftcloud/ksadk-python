"""
agentengine invoke - 与已部署的 Agent 进行交互

支持 OpenAI 兼容格式调用，支持流式输出
"""

import click
import asyncio
import io
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional
import time
import uuid
from ksadk.api import AgentEngineAPIError, AgentEngineClient
from ksadk.cli.agent_ref import merge_agent_inputs, resolve_agent_ref, resolve_openclaw_ref
from ksadk.cli.cmd_files import (
    _build_sync_payload,
    _collect_local_files_report,
    _emit_sync_payload,
    _format_size,
    _normalize_workspace_dir,
    _push_workspace_files,
)
from ksadk.cli.resource_common import CONTEXT_SETTINGS, CompatibilityAliasCommand, print_compatibility_hint
from ksadk.hermes_terminal import run_hermes_terminal_session
from ksadk.terminal_client import run_terminal_session
from ksadk_runtime_common.workspace_files.constants import DEFAULT_WORKSPACE_MAX_UPLOAD_BYTES

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.live import Live
    console = Console()
except ImportError:
    console = None
    Markdown = None
    Live = None


@click.command(
    context_settings=CONTEXT_SETTINGS,
    hidden=True,
    cls=CompatibilityAliasCommand,
    canonical_command="agentengine agent invoke",
)
@click.argument("agent_ref", required=False)
@click.option("--agent", "--agent-id", "agent_option", "-a", help="Agent 名称或 ID")
@click.option("--endpoint", "-e", help="Agent Endpoint URL (覆盖自动获取)")
@click.option("--api-key", help="AgentEngine API Key (覆盖本地配置)")
@click.option(
    "--gateway-token",
    "openclaw_gateway_token",
    envvar="OPENCLAW_GATEWAY_TOKEN",
    help="OpenClaw Gateway token/password（用于 OpenClaw token/password 模式的 /v1/responses）",
)
@click.option("--message", "-m", help="发送的消息 (单次调用模式)")
@click.option("--session", "-s", help="Session ID (可选)")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--local", "-l", is_flag=True, help="连接本地服务 (http://localhost:8080)")
@click.option("--insecure", "-k", is_flag=True, help="跳过 SSL 证书验证 (类似 curl -k)")
@click.option(
    "--transport",
    type=click.Choice(["auto", "chat", "native"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="交互传输层: auto(自动), chat(HTTP OpenAI API), native(框架原生远端终端)",
)
@click.option(
    "--local-workspace",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="仅 Hermes 远程 native 模式: 先将本地目录同步到远端 workspace",
)
@click.option(
    "--remote-workspace-path",
    help="远端 workspace 子目录 (默认使用本地目录名)",
)
@click.option(
    "--verbose-workspace-sync",
    is_flag=True,
    help="显示逐文件 workspace 同步日志（默认使用单行进度刷新）",
)
@click.option("--model", help="指定模型名称")
@click.option("--show-thinking", is_flag=True, help="显示模型思考过程")
def invoke(
    agent_ref: str,
    agent_option: str,
    endpoint: str,
    api_key: str,
    openclaw_gateway_token: str,
    message: str,
    session: str,
    region: str,
    local: bool,
    insecure: bool,
    transport: str,
    local_workspace: Path | None,
    remote_workspace_path: str | None,
    verbose_workspace_sync: bool,
    model: str,
    show_thinking: bool,
):
    """与 Agent 进行交互 (本地或远程)。"""
    run_invoke_command(
        agent_ref=agent_ref,
        agent_option=agent_option,
        endpoint=endpoint,
        api_key=api_key,
        openclaw_gateway_token=openclaw_gateway_token,
        message=message,
        session=session,
        region=region,
        local=local,
        insecure=insecure,
        transport=transport,
        local_workspace=local_workspace,
        remote_workspace_path=remote_workspace_path,
        verbose_workspace_sync=verbose_workspace_sync,
        model=model,
        show_thinking=show_thinking,
        compatibility_alias=True,
    )


def _resolve_remote_workspace_seed_path(
    local_workspace: Path,
    remote_workspace_path: str | None,
) -> str:
    if remote_workspace_path:
        return _normalize_workspace_dir(remote_workspace_path)
    default_name = local_workspace.resolve().name or local_workspace.name or "workspace"
    return _normalize_workspace_dir(default_name)


def _summarize_local_workspace_dir(
    local_dir: Path,
    *,
    ignore_git_artifacts: bool,
) -> dict[str, Any]:
    if not local_dir.exists():
        raise click.ClickException(f"本地目录不存在: {local_dir}")
    if not local_dir.is_dir():
        raise click.ClickException(f"本地路径不是目录: {local_dir}")

    report = _collect_local_files_report(
        local_dir,
        ignore_dev_artifacts=True,
        ignore_git_artifacts=ignore_git_artifacts,
    )
    files = [
        {
            "path": file_path,
            "relative_path": relative_path,
            "size_bytes": int(file_path.stat().st_size),
        }
        for relative_path, file_path in report["files"]
    ]
    return {
        "local_dir": local_dir,
        "files": files,
        "total_files": len(files),
        "total_bytes": int(report["total_bytes"]),
        "ignored_artifacts": list(report["ignored_artifacts"]),
    }


def _describe_workspace_sync_phase(event: dict[str, Any] | None) -> str:
    phase = str((event or {}).get("phase") or "").strip()
    remote_path = str((event or {}).get("remote_path") or "").strip()
    if phase == "list_start":
        return f"列出远端目录 {remote_path or '.'}"
    if phase == "upload_start":
        return f"上传 {remote_path or '文件'}"
    if phase == "upload_done":
        return f"确认上传 {remote_path or '文件'}"
    if phase == "limit_done":
        return "读取 workspace 上传限制"
    if phase == "scan_done":
        return "扫描本地目录"
    return "同步 workspace"


def _render_workspace_sync_progress_bar(current: object | None, total: object | None, *, width: int = 20) -> str:
    try:
        current_value = int(current or 0)
        total_value = max(int(total or 0), 1)
    except (TypeError, ValueError):
        current_value = 0
        total_value = 1
    current_value = max(0, min(current_value, total_value))
    filled = int(width * current_value / total_value)
    return f"[{'#' * filled}{'-' * (width - filled)}]"


def _format_workspace_sync_percent(current: object | None, total: object | None) -> str:
    try:
        current_value = float(current or 0)
        total_value = max(float(total or 0), 1.0)
    except (TypeError, ValueError):
        current_value = 0.0
        total_value = 1.0
    percent = max(0.0, min(current_value * 100.0 / total_value, 100.0))
    return f"{percent:.1f}".rstrip("0").rstrip(".") + "%"


def _build_workspace_sync_progress_emitter(*, verbose: bool) -> Callable[[dict[str, Any]], None]:
    active_inline = False
    last_inline_width = 0

    def _finish_inline() -> None:
        nonlocal active_inline, last_inline_width
        if active_inline:
            click.echo(f"\r{' ' * last_inline_width}\r", nl=False)
            click.echo("")
            active_inline = False
            last_inline_width = 0

    def _emit(event: dict[str, Any]) -> None:
        nonlocal active_inline, last_inline_width

        phase = str(event.get("phase") or "").strip()
        if phase == "scan_done":
            _finish_inline()
            click.secho(
                "📂 准备同步 workspace: "
                f"{event.get('total_files', 0)} 个文件，"
                f"{_format_size(event.get('total_bytes', 0))} -> workspace:/{event.get('remote_path', '.')}",
                fg="blue",
            )
            ignored_artifacts = list(event.get("ignored_artifacts") or [])
            if ignored_artifacts:
                click.echo(f"   默认忽略: {', '.join(ignored_artifacts)}")
            return
        if phase == "limit_done":
            _finish_inline()
            click.echo(f"   上传上限: {_format_size(event.get('max_upload_bytes'))}")
            return
        if phase == "list_start":
            _finish_inline()
            click.echo(f"   检查远端目录: workspace:/{event.get('remote_path', '.')}")
            return
        if phase == "list_done":
            _finish_inline()
            click.echo(f"   远端已有条目: {event.get('remote_entry_count', 0)}")
            return
        if phase == "upload_start":
            total = max(int(event.get("total") or 0), 1)
            current = max(0, min(int(event.get("current") or 0), total))
            message = (
                "   "
                f"{_render_workspace_sync_progress_bar(current, total)} "
                f"{_format_workspace_sync_percent(current, total)} ({current}/{total}) "
                f"上传 {event.get('remote_path', '')} "
                f"({_format_size(event.get('size_bytes'))})"
            )
            if verbose:
                click.echo(message)
            else:
                padded_message = message.ljust(last_inline_width)
                click.echo(f"\r{padded_message}", nl=False)
                active_inline = True
                last_inline_width = max(last_inline_width, len(message))
            return
        if phase == "upload_skipped":
            total = max(int(event.get("total") or 0), 1)
            current = max(0, min(int(event.get("current") or 0), total))
            message = (
                "   "
                f"{_render_workspace_sync_progress_bar(current, total)} "
                f"{_format_workspace_sync_percent(current, total)} ({current}/{total}) "
                f"跳过已存在 {event.get('remote_path', '')}"
            )
            if verbose:
                click.echo(message)
            else:
                padded_message = message.ljust(last_inline_width)
                click.echo(f"\r{padded_message}", nl=False)
                active_inline = True
                last_inline_width = max(last_inline_width, len(message))
            return
        if phase == "upload_done":
            total = max(int(event.get("total") or 0), 1)
            current = max(0, min(int(event.get("current") or 0), total))
            if not verbose and current >= total:
                _finish_inline()
            return

    return _emit


def _emit_workspace_sync_progress(event: dict[str, Any]) -> None:
    if not hasattr(_emit_workspace_sync_progress, "_emitter"):
        _emit_workspace_sync_progress._emitter = _build_workspace_sync_progress_emitter(  # type: ignore[attr-defined]
            verbose=True
        )
    _emit_workspace_sync_progress._emitter(event)  # type: ignore[attr-defined]


def _extract_workspace_upload_limit(bootstrap: dict[str, Any] | None) -> int | None:
    if not isinstance(bootstrap, dict):
        return None
    workspace = bootstrap.get("workspace_files") or bootstrap.get("WorkspaceFiles") or {}
    if not isinstance(workspace, dict):
        return None
    raw_limit = workspace.get("max_upload_bytes") or workspace.get("MaxUploadBytes")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return None
    return limit if limit > 0 else None


async def _lookup_workspace_upload_limit(
    *,
    agent_ref: str | None,
    region: str,
) -> int:
    if not agent_ref:
        return DEFAULT_WORKSPACE_MAX_UPLOAD_BYTES

    try:
        async with AgentEngineClient(region=region) as client:
            if str(agent_ref).startswith("ar-"):
                payload = await client.get_agent_ui_bootstrap(agent_id=agent_ref)
            else:
                payload = await client.get_agent_ui_bootstrap(name=agent_ref)
    except Exception:
        return DEFAULT_WORKSPACE_MAX_UPLOAD_BYTES

    return _extract_workspace_upload_limit(payload) or DEFAULT_WORKSPACE_MAX_UPLOAD_BYTES


async def _sync_local_workspace_for_hermes_invoke(
    *,
    agent_ref: str | None,
    local_workspace: str | Path,
    remote_path: str,
    region: str,
    endpoint: str | None,
    api_key: str | None,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    local_dir = Path(local_workspace).expanduser().resolve()
    max_upload_bytes = await _lookup_workspace_upload_limit(agent_ref=agent_ref, region=region)
    if progress_callback:
        progress_callback(
            {
                "phase": "limit_done",
                "remote_path": remote_path,
                "max_upload_bytes": max_upload_bytes,
            }
        )
    summary = _summarize_local_workspace_dir(local_dir, ignore_git_artifacts=False)
    if summary["total_bytes"] > max_upload_bytes:
        summary_without_git = _summarize_local_workspace_dir(local_dir, ignore_git_artifacts=True)
        if summary_without_git["total_bytes"] <= max_upload_bytes:
            summary = summary_without_git
    if progress_callback:
        progress_callback(
            {
                "phase": "scan_done",
                "local_dir": str(local_dir),
                "remote_path": remote_path,
                "total_files": summary["total_files"],
                "total_bytes": summary["total_bytes"],
                "ignored_artifacts": summary.get("ignored_artifacts", []),
            }
        )

    if summary["total_files"] == 0:
        raise click.ClickException(f"本地目录为空，当前版本暂不支持同步纯空目录: {local_dir}")

    for item in summary["files"]:
        if item["size_bytes"] > max_upload_bytes:
            raise click.ClickException(
                "本地目录中存在超过远端 workspace 上传上限的文件："
                f"{item['path']}（{_format_size(item['size_bytes'])} > {_format_size(max_upload_bytes)}）"
            )

    if summary["total_bytes"] > max_upload_bytes:
        raise click.ClickException(
            "本地目录总大小超过远端 workspace 单次同步上限："
            f"{_format_size(summary['total_bytes'])} > {_format_size(max_upload_bytes)}"
        )

    last_progress: dict[str, Any] | None = None

    def _record_progress(event: dict[str, Any]) -> None:
        nonlocal last_progress
        last_progress = dict(event)
        if progress_callback:
            progress_callback(event)

    try:
        return await _push_workspace_files(
            agent_ref=agent_ref,
            local_dir=local_dir,
            remote_path=remote_path,
            force=True,
            region=region,
            endpoint=endpoint,
            api_key=api_key,
            progress_callback=_record_progress,
            ignore_dev_artifacts=True,
            ignore_git_artifacts=".git" in list(summary.get("ignored_artifacts") or []),
        )
    except AgentEngineAPIError as exc:
        phase = _describe_workspace_sync_phase(last_progress)
        raise click.ClickException(
            f"同步远端 workspace 失败（{phase}）：{exc.message}"
        ) from exc


def run_invoke_command(
    *,
    agent_ref: str | None,
    agent_option: str | None,
    endpoint: str | None,
    api_key: str | None,
    message: str | None,
    session: str | None,
    region: str,
    local: bool,
    insecure: bool,
    transport: str,
    local_workspace: str | Path | None = None,
    remote_workspace_path: str | None = None,
    verbose_workspace_sync: bool = False,
    model: str | None,
    show_thinking: bool,
    openclaw_gateway_token: str | None = None,
    compatibility_alias: bool = False,
):
    """与 Agent 进行交互 (本地或远程)。"""
    if compatibility_alias:
        print_compatibility_hint(
            legacy="agentengine invoke",
            canonical="agentengine agent invoke",
        )
    try:
        agent_input = merge_agent_inputs(
            agent_option=agent_option,
            positional_agent=agent_ref,
        )
    except ValueError as e:
        click.secho(f"❌ {e}", fg="red")
        raise SystemExit(1)

    if remote_workspace_path and not local_workspace:
        click.secho("❌ --remote-workspace-path 需要与 --local-workspace 一起使用。", fg="red")
        raise SystemExit(1)
    if local_workspace and message:
        click.secho("❌ --local-workspace 仅支持远程 Hermes native 交互模式。", fg="red")
        raise SystemExit(1)

    # 加载本地状态
    state = _load_state()
    latest_access: dict[str, Any] = {}
    reuse_state_session = True
    target_agent: str | None = None
    
    normalized_transport = (transport or "auto").strip().lower() or "auto"

    # 确定 Endpoint
    if local:
        endpoint = "http://localhost:8080"
    elif not endpoint:
        resolved = resolve_agent_ref(
            agent_input,
            cwd=Path("."),
            include_state=True,
            include_project_config=True,
        )
        if not resolved and not agent_input:
            resolved = resolve_openclaw_ref(
                None,
                cwd=Path("."),
                include_state=True,
            )
        if not resolved:
            click.secho("❌ 请指定 Agent（--agent 或位置参数）、--local 或 --endpoint", fg="red")
            click.echo("   自动解析顺序: .agentengine.state -> agentengine.yaml/ksadk.yaml")
            raise SystemExit(1)
        target_agent = resolved.value
        if resolved.source != "cli":
            click.echo(f"ℹ 未显式指定 Agent，使用 {resolved.source_text}: {target_agent}")

        latest_access = _refresh_remote_access(
            target_agent=target_agent,
            region=region,
            state=state,
            persist=_state_matches_target(state, target_agent),
        )
        reuse_state_session = not agent_input or _state_matches_target(state, target_agent)

        # 优先使用 state 里的 endpoint (如果是对应的 agent)
        if latest_access.get("endpoint"):
            endpoint = latest_access["endpoint"]
        elif not agent_input or _state_matches_target(state, target_agent):
            endpoint = state.get("endpoint")
            
        if not endpoint:
            # 自动获取
            endpoint = _get_endpoint(target_agent, region)

    # API Key
    api_key = api_key or latest_access.get("api_key") or state.get("api_key")
    session_id = (
        session
        or (state.get("session_id") if reuse_state_session else None)
        or str(uuid.uuid4())[:8]
    )

    next_state = _load_state() or dict(state)
    for key in ("agent_id", "name", "endpoint", "api_key", "framework"):
        if latest_access.get(key):
            next_state[key] = latest_access[key]
    if latest_access.get("framework") == "hermes":
        next_state["type"] = "hermes"
    if latest_access.get("framework") == "openclaw":
        next_state["type"] = "openclaw"
    next_state["session_id"] = session_id
    _save_state(next_state)
    runtime_api_key = _select_runtime_api_key(
        api_key=api_key,
        openclaw_gateway_token=openclaw_gateway_token,
        state=next_state,
        latest_access=latest_access,
    )

    click.secho(f"🤖 连接到 Agent", fg="blue", bold=True)
    click.echo(f"   Endpoint: {endpoint}")
    if _is_openclaw_target(next_state, latest_access):
        if runtime_api_key:
            click.echo(f"   Runtime Auth: OpenClaw Gateway token {_mask_secret(runtime_api_key)}")
        else:
            click.echo("   Runtime Auth: OpenClaw Gateway anonymous/trusted-proxy")
        if api_key:
            click.echo(f"   AgentEngine API: Bearer {_mask_secret(api_key)}")
    elif api_key:
        click.echo(f"   Auth:     Bearer {_mask_secret(api_key)}")
    else:
        click.secho("   ⚠️  未发现 API Key，尝试匿名调用", fg="yellow")
    
    if insecure:
        click.secho("   ⚠️  SSL 证书验证已禁用", fg="yellow")

    if message:
        # 单次调用模式
        api_format = asyncio.run(
            _resolve_remote_api_format(
                endpoint=endpoint,
                api_key=api_key,
                runtime_api_key=runtime_api_key,
                insecure=insecure,
                state=next_state,
                latest_access=latest_access,
            )
        )
        asyncio.run(_invoke_once(endpoint, message, runtime_api_key, session_id, True, insecure, model, api_format))
    else:
        is_hermes_target = _is_hermes_target(next_state, latest_access)
        is_openclaw_target = _is_openclaw_target(next_state, latest_access)
        if normalized_transport == "chat" and is_hermes_target:
            click.secho("❌ Hermes 不再支持 ksadk 通用 chat TUI。", fg="red")
            click.echo("   浏览器聊天页请改用: agentengine hermes open --chat")
            raise SystemExit(1)
        if local_workspace:
            if local or normalized_transport == "chat":
                click.secho("❌ --local-workspace 仅支持远程 Hermes native 交互模式。", fg="red")
                raise SystemExit(1)
            if not is_hermes_target:
                click.secho("❌ --local-workspace 仅支持 Hermes 远程 native 模式。", fg="red")
                raise SystemExit(1)

            local_workspace_dir = Path(local_workspace).expanduser().resolve()
            normalized_remote_workspace_path = _resolve_remote_workspace_seed_path(
                local_workspace_dir,
                remote_workspace_path,
            )
            sync_agent_ref = (
                latest_access.get("agent_id")
                or next_state.get("agent_id")
                or target_agent
                or latest_access.get("name")
                or next_state.get("name")
            )
            sync_payload = asyncio.run(
                _sync_local_workspace_for_hermes_invoke(
                    agent_ref=sync_agent_ref,
                    local_workspace=local_workspace_dir,
                    remote_path=normalized_remote_workspace_path,
                    region=region,
                    endpoint=endpoint,
                    api_key=api_key,
                    progress_callback=_build_workspace_sync_progress_emitter(
                        verbose=verbose_workspace_sync
                    ),
                )
            )
            _emit_sync_payload(_build_sync_payload(sync_payload), None)
        else:
            normalized_remote_workspace_path = None
        # 默认 TUI 模式
        if _should_use_hermes_native_tui(
            transport=normalized_transport,
            local=local,
            state=next_state,
            latest_access=latest_access,
        ):
            native_kwargs: dict[str, Any] = {
                "endpoint": endpoint,
                "api_key": api_key,
                "session_id": session_id,
                "insecure": insecure,
            }
            if normalized_remote_workspace_path is not None:
                native_kwargs["cwd"] = normalized_remote_workspace_path
            _invoke_hermes_terminal_tui(
                **native_kwargs,
            )
        elif _should_use_openclaw_native_tui(
            transport=normalized_transport,
            local=local,
            state=next_state,
            latest_access=latest_access,
        ):
            _invoke_openclaw_terminal_tui(
                endpoint=endpoint,
                api_key=runtime_api_key,
                session_id=session_id,
                insecure=insecure,
            )
        else:
            api_format = asyncio.run(
                _resolve_remote_api_format(
                    endpoint=endpoint,
                    api_key=api_key,
                    runtime_api_key=runtime_api_key,
                    insecure=insecure,
                    state=next_state,
                    latest_access=latest_access,
                )
            )
            _invoke_tui(
                endpoint,
                runtime_api_key,
                session_id,
                insecure,
                model,
                show_thinking,
                api_format=api_format,
                responses_session_header=(
                    "x-openclaw-session-key" if _is_openclaw_target(next_state, latest_access) else None
                ),
            )




def _invoke_tui(
    endpoint: str,
    api_key: str = None,
    session_id: str = None,
    insecure: bool = False,
    model: str = None,
    show_thinking: bool = False,
    api_format: str = "chat_completions",
    responses_session_header: str | None = None,
):
    """使用 TUI 模式调用"""
    from ksadk.runners.remote_runner import RemoteRunner
    from ksadk.tui import AgentTUI

    runner = RemoteRunner(
        endpoint=endpoint,
        api_key=api_key,
        session_id=session_id,
        insecure=insecure,
        model=model,
        api_format=api_format,
        responses_session_header=responses_session_header,
    )

    app = AgentTUI(
        runner=runner,
        show_thinking=show_thinking,
        project_dir=".",
    )
    app.run()


def _load_state() -> dict:
    """从 .agentengine.state 加载状态"""
    import yaml
    state_file = Path(".") / ".agentengine.state"
    if state_file.exists():
        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return {}


def _save_state(state: dict) -> None:
    """保存 .agentengine.state。"""
    import yaml

    state_file = Path(".") / ".agentengine.state"
    payload = dict(state)
    payload["updated_at"] = datetime.now().isoformat()
    with open(state_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, allow_unicode=True)


def _state_matches_target(state: dict, target_agent: str | None) -> bool:
    if not state or not target_agent:
        return False
    return target_agent == state.get("agent_id") or target_agent == state.get("name")


def _extract_remote_access(detail: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(detail, dict):
        return {}

    basic = detail.get("basic") if isinstance(detail.get("basic"), dict) else {}
    quick = detail.get("quick_access") if isinstance(detail.get("quick_access"), dict) else {}

    deployment = detail.get("deployment") if isinstance(detail.get("deployment"), dict) else {}
    framework = (
        deployment.get("framework")
        or basic.get("framework")
        or detail.get("framework")
    )

    return {
        "agent_id": basic.get("agent_id") or detail.get("agent_id"),
        "name": basic.get("name") or detail.get("name"),
        "endpoint": quick.get("public_endpoint")
        or quick.get("private_endpoint")
        or detail.get("endpoint"),
        "api_key": quick.get("api_key") or detail.get("api_key"),
        "framework": str(framework or "").strip().lower() or None,
    }


async def _fetch_remote_access(target_agent: str, region: str) -> Dict[str, Any]:
    from ksadk.api import AgentEngineClient

    async with AgentEngineClient(region=region) as client:
        try:
            return _extract_remote_access(await client.get_agent(agent_id=target_agent, include_api_key=True))
        except Exception:
            return _extract_remote_access(await client.get_agent(name=target_agent, include_api_key=True))


def _refresh_remote_access(
    *,
    target_agent: str,
    region: str,
    state: dict,
    persist: bool,
) -> Dict[str, Any]:
    try:
        latest = asyncio.run(_fetch_remote_access(target_agent, region))
    except Exception:
        return {}

    if persist and latest:
        merged = dict(state)
        for key in ("agent_id", "name", "endpoint", "api_key", "framework"):
            if latest.get(key):
                merged[key] = latest[key]
        if latest.get("framework") == "hermes":
            merged["type"] = "hermes"
        if latest.get("framework") == "openclaw":
            merged["type"] = "openclaw"
        _save_state(merged)
    return latest


def _is_hermes_target(state: dict, latest_access: dict) -> bool:
    framework = str(
        latest_access.get("framework")
        or state.get("framework")
        or state.get("type")
        or ""
    ).strip().lower()
    return framework == "hermes"


def _is_openclaw_target(state: dict, latest_access: dict) -> bool:
    framework = str(
        latest_access.get("framework")
        or state.get("framework")
        or state.get("type")
        or ""
    ).strip().lower()
    return framework == "openclaw"


def _mask_secret(secret: str | None) -> str:
    if not secret:
        return ""
    text = str(secret)
    if len(text) <= 4:
        return "****"
    return f"{text[:4]}****"


def _openclaw_auth_mode(state: dict, latest_access: dict) -> str:
    return str(
        latest_access.get("openclaw_auth_mode")
        or latest_access.get("gateway_auth_mode")
        or state.get("openclaw_auth_mode")
        or state.get("gateway_auth_mode")
        or ""
    ).strip().lower()


def _select_runtime_api_key(
    *,
    api_key: str | None,
    openclaw_gateway_token: str | None,
    state: dict,
    latest_access: dict,
) -> str | None:
    if not _is_openclaw_target(state, latest_access):
        return api_key

    gateway_token = (
        openclaw_gateway_token
        or os.environ.get("OPENCLAW_GATEWAY_TOKEN")
        or os.environ.get("OPENCLAW_GATEWAY_PASSWORD")
        or latest_access.get("openclaw_gateway_token")
        or latest_access.get("openclaw_gateway_password")
        or state.get("openclaw_gateway_token")
        or state.get("openclaw_gateway_password")
    )
    if gateway_token:
        return str(gateway_token).strip() or None

    auth_mode = _openclaw_auth_mode(state, latest_access)
    if auth_mode in {"token", "password"}:
        raise click.ClickException(
            "当前 OpenClaw Gateway 为 token/password 模式，agentengine invoke 需要 OpenClaw Gateway token。\n"
            "请使用: agentengine invoke --gateway-token <token>\n"
            "或设置: OPENCLAW_GATEWAY_TOKEN=<token> agentengine invoke\n"
            "如果部署时传过 OPENCLAW_GATEWAY_TOKEN，请重新运行部署让本地 .agentengine.state 记录该 token。\n"
            "注意：这里不是 AgentEngine API Key（ak-*），而是 OPENCLAW_GATEWAY_TOKEN/OPENCLAW_GATEWAY_PASSWORD。"
        )

    return api_key


def _select_remote_api_format(state: dict, latest_access: dict) -> str:
    framework = str(
        latest_access.get("framework")
        or state.get("framework")
        or state.get("type")
        or ""
    ).strip().lower()
    if framework in {"openclaw", "hermes"}:
        return "responses"
    return "chat_completions"


async def _resolve_remote_api_format(
    *,
    endpoint: str,
    api_key: str | None,
    runtime_api_key: str | None = None,
    insecure: bool,
    state: dict,
    latest_access: dict,
) -> str:
    api_format = _select_remote_api_format(state, latest_access)
    if api_format != "responses" or not _is_openclaw_target(state, latest_access):
        return api_format

    probe_api_key = runtime_api_key if runtime_api_key is not None else api_key
    if await _probe_openclaw_responses_route(endpoint=endpoint, api_key=probe_api_key, insecure=insecure):
        return api_format

    raise click.ClickException(
        "当前 OpenClaw endpoint 未暴露 /v1/responses，不能使用 agentengine invoke 的 HTTP TUI。\n"
        "可先使用: agentengine dashboard open\n"
        "如果要命令行交互，请升级/修复 OpenClaw 镜像或 bootstrap，使 Gateway 暴露 OpenResponses API。"
    )


async def _probe_openclaw_responses_route(*, endpoint: str, api_key: str | None, insecure: bool) -> bool:
    try:
        import httpx
    except ImportError:
        return False

    url = f"{endpoint.rstrip('/')}/v1/responses"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    client_kwargs: dict[str, Any] = {"timeout": 5, "trust_env": True}
    if insecure:
        client_kwargs["verify"] = False

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.get(url, headers=headers)
    except Exception:
        return False

    # POST-only API routes usually answer GET with 405/422/400. A 404 or HTML 200
    # means the request fell through to the OpenClaw UI/router instead of the API.
    return response.status_code in {400, 401, 405, 422}


def _should_use_hermes_native_tui(*, transport: str, local: bool, state: dict, latest_access: dict) -> bool:
    if transport == "chat":
        return False
    if transport == "native":
        return True
    if local:
        return False
    return _is_hermes_target(state, latest_access)


def _should_use_openclaw_native_tui(*, transport: str, local: bool, state: dict, latest_access: dict) -> bool:
    if transport == "chat":
        return False
    if transport == "native":
        return True
    if local:
        return False
    return _is_openclaw_target(state, latest_access)


def _invoke_hermes_terminal_tui(
    endpoint: str,
    api_key: str = None,
    session_id: str = None,
    insecure: bool = False,
    cwd: str | None = None,
):
    click.secho("🖥️  Hermes Native Remote TUI", fg="blue", bold=True)
    click.echo("   退出: Ctrl-D 或 Ctrl-C")
    click.echo("   新 Pod 首次启动会加载 Hermes tools/skills，可能需要几十秒到 1 分钟；后续进入会更快。")
    try:
        _warmup_hermes_terminal(
            endpoint=endpoint,
            api_key=api_key,
            session_id=session_id,
            insecure=insecure,
        )
        exit_code = asyncio.run(
            run_hermes_terminal_session(
                endpoint=endpoint,
                api_key=api_key,
                session_id=session_id,
                insecure=insecure,
                mode="tui",
                argv=[],
                cwd=cwd,
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise SystemExit(130)
    except Exception as e:
        click.secho(f"\n❌ Hermes 终端连接失败: {e}", fg="red")
        click.echo("   浏览器聊天页请改用: agentengine hermes open --chat")
        raise SystemExit(1)
    if exit_code:
        raise SystemExit(exit_code)


def _warmup_hermes_terminal(
    *,
    endpoint: str,
    api_key: str | None,
    session_id: str | None,
    insecure: bool,
) -> None:
    try:
        asyncio.run(
            asyncio.wait_for(
                run_hermes_terminal_session(
                    endpoint=endpoint,
                    api_key=api_key,
                    session_id=session_id,
                    insecure=insecure,
                    mode="exec",
                    argv=["status"],
                    stdin=io.BytesIO(b""),
                    stdout=io.BytesIO(),
                ),
                timeout=30,
            )
        )
    except Exception as exc:
        if os.getenv("AGENTENGINE_HERMES_TUI_DEBUG"):
            click.echo(f"   Hermes terminal warmup skipped: {exc}", err=True)


def _invoke_openclaw_terminal_tui(
    endpoint: str,
    api_key: str = None,
    session_id: str = None,
    insecure: bool = False,
):
    click.secho("🖥️  OpenClaw Native Remote TUI", fg="blue", bold=True)
    click.echo("   退出: Ctrl-D 或 Ctrl-C")
    try:
        exit_code = asyncio.run(
            run_terminal_session(
                endpoint=endpoint,
                api_key=api_key,
                session_id=session_id,
                insecure=insecure,
                mode="tui",
                argv=[],
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise SystemExit(130)
    except Exception as e:
        click.secho(f"\n❌ OpenClaw 终端连接失败: {e}", fg="red")
        click.echo("   浏览器聊天页请改用: agentengine dashboard open --path /chat")
        raise SystemExit(1)
    if exit_code:
        raise SystemExit(exit_code)


def _get_api_key() -> Optional[str]:
    """兼容旧代码"""
    return _load_state().get("api_key")


def _get_endpoint(agent_ref: str, region: str) -> str:
    """获取 Agent Endpoint（先按 ID，再按名称）"""
    from ksadk.api import AgentEngineClient
    import asyncio

    async def _get():
        async with AgentEngineClient(region=region) as client:
            # 1) 优先按 ID 查询
            try:
                res = await client.get_agent(agent_id=agent_ref)
                endpoint = _extract_remote_access(res).get("endpoint", "")
                if endpoint:
                    return endpoint
            except Exception:
                pass

            # 2) 回退按名称查询
            res = await client.get_agent(name=agent_ref)
            endpoint = _extract_remote_access(res).get("endpoint", "")
            if endpoint:
                return endpoint

            # endpoint 为空时，尽量提取真实 ID 供默认域名拼接
            basic = res.get("basic", {})
            return basic.get("agent_id") or res.get("agent_id") or ""

    try:
        resolved = asyncio.run(_get())
        if resolved and resolved.startswith("http"):
            return resolved
        if resolved:
            click.secho(f"⚠️  Agent '{agent_ref}' 未返回 Endpoint，尝试默认域名", fg="yellow")
            return f"https://{resolved}.agent.kspmas.ksyun.com"
        click.secho(f"⚠️  Agent '{agent_ref}' 未返回 Endpoint，尝试使用默认格式", fg="yellow")
        return f"https://{agent_ref}.agent.kspmas.ksyun.com"
    except Exception as e:
        # 如果是本地开发环境或者连接失败，降级处理
        click.secho(f"⚠️  获取 Endpoint 失败: {e}，尝试使用默认格式", fg="yellow")
        return f"https://{agent_ref}.agent.kspmas.ksyun.com"


async def _invoke_once(
    endpoint: str,
    message: str,
    api_key: str = None,
    session_id: str = None,
    stream: bool = True,
    insecure: bool = False,
    model: str = None,
    api_format: str = "chat_completions",
):
    """单次调用"""
    click.echo(f"\n👤 你: {message}")
    click.echo(f"🤖 Agent: ", nl=False)

    try:
        if stream:
            full_response = ""
            if Live and Markdown:
                # 降低刷新率减少闪烁，vertical_overflow="visible"防止回滚丢失
                # 手动控制刷新以减少闪烁
                with Live(Markdown("", justify="left"), console=console, auto_refresh=False, vertical_overflow="visible") as live:
                    last_refresh_time = 0
                    full_reasoning = ""
                    async for chunk in _stream_chat(endpoint, message, api_key, session_id, True, insecure, model, api_format):
                        content, reasoning = _extract_content(chunk)
                        
                        updated = False
                        if reasoning:
                            full_reasoning += reasoning
                            updated = True
                        if content:
                            full_response += content
                            updated = True
                            
                        if updated:
                            # 构造显示文本
                            display_text = ""
                            if full_reasoning:
                                formatted_reasoning = full_reasoning.replace('\n', '\n> ')
                                display_text += f"> 🧠 **Thinking:**\n> {formatted_reasoning}\n\n"
                            display_text += full_response
                            
                            live.update(Markdown(display_text, justify="left"))
                            
                            # 基于时间限流刷新 (每0.2秒一次 = 5 FPS)
                            now = time.time()
                            if now - last_refresh_time > 0.2:
                                live.refresh()
                                last_refresh_time = now
                    live.refresh() # 确保最后一次刷新
            else:
               async for chunk in _stream_chat(endpoint, message, api_key, session_id, True, insecure, model, api_format):
                    content, reasoning = _extract_content(chunk)
                    if reasoning:
                        click.secho(reasoning, fg="bright_black", nl=False)
                    if content:
                        print(content, end="", flush=True)
            click.echo()  # 换行
        else:
            response = await _chat(endpoint, message, api_key, session_id, insecure, model, api_format)
            content = _extract_response_content(response)
            if console and Markdown:
                console.print(Markdown(content))
            else:
                click.echo(content)
    except Exception as e:
        click.secho(f"\n❌ 调用失败: {e}", fg="red")



async def _chat(
    endpoint: str,
    message: str,
    api_key: str = None,
    session_id: str = None,
    insecure: bool = False,
    model: str = None,
    api_format: str = "chat_completions",
) -> dict:
    """非流式调用 (OpenAI 兼容格式)"""
    try:
        import httpx
    except ImportError:
        click.secho("❌ 请安装 httpx: pip install httpx", fg="red")
        raise SystemExit(1)

    normalized_api_format = str(api_format or "chat_completions").strip().lower()
    if normalized_api_format == "responses":
        url = f"{endpoint.rstrip('/')}/v1/responses"
        payload = {"input": [{"role": "user", "content": message}], "stream": False}
    else:
        url = f"{endpoint.rstrip('/')}/v1/chat/completions"
        payload = {"messages": [{"role": "user", "content": message}], "stream": False}

    if session_id:
        payload["session_id"] = session_id

    if model:
        payload["model"] = model

    # 本地请求禁用系统代理 (ClashX 等会导致本地请求 502 错误)
    # trust_env=False 会禁用: 代理设置、SSL 证书环境变量、.netrc 文件
    # 对本地请求通常无影响，因为不需要这些配置
    is_local = "localhost" in url or "127.0.0.1" in url or "0.0.0.0" in url

    # 构造 httpx client 配置
    client_kwargs = {"timeout": 60, "trust_env": not is_local}

    # 如果指定了 --insecure 参数，跳过 SSL 证书验证（类似 curl -k）
    if insecure:
        client_kwargs["verify"] = False

    # 构造 Headers
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()


async def _stream_chat(
    endpoint: str,
    message: str,
    api_key: str = None,
    session_id: str = None,
    is_once: bool = False,
    insecure: bool = False,
    model: str = None,
    api_format: str = "chat_completions",
):
    """流式调用 (SSE)"""
    try:
        import httpx
    except ImportError:
        click.secho("❌ 请安装 httpx: pip install httpx", fg="red")
        raise SystemExit(1)

    normalized_api_format = str(api_format or "chat_completions").strip().lower()
    if normalized_api_format == "responses":
        url = f"{endpoint.rstrip('/')}/v1/responses"
        payload = {"input": [{"role": "user", "content": message}], "stream": True}
    else:
        url = f"{endpoint.rstrip('/')}/v1/chat/completions"
        payload = {"messages": [{"role": "user", "content": message}], "stream": True}

    if session_id:
        payload["session_id"] = session_id

    if model:
        payload["model"] = model

    # 本地请求禁用系统代理 (ClashX 等会导致本地请求 502 错误)
    # trust_env=False 会禁用: 代理设置、SSL 证书环境变量、.netrc 文件
    # 对本地请求通常无影响，因为不需要这些配置
    is_local = "localhost" in url or "127.0.0.1" in url or "0.0.0.0" in url

    # 构造 httpx client 配置
    client_kwargs = {"timeout": 60, "trust_env": not is_local}

    # 如果指定了 --insecure 参数，跳过 SSL 证书验证（类似 curl -k）
    if insecure:
        client_kwargs["verify"] = False

    # 构造 Headers
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(**client_kwargs) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as response:
            response.raise_for_status()
            try:
                current_event = ""
                # Use aiter_lines() for robust UTF-8 decoding and line splitting
                async for line in response.aiter_lines():
                    if not line:
                        continue

                    if line.startswith("event: "):
                        current_event = line[7:].strip()
                        continue

                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break

                        try:
                            data = json.loads(data_str)
                            if current_event and isinstance(data, dict):
                                data = {**data, "_event": current_event}
                            # 直接 yield 解析后的 JSON 数据，让 _extract_content 处理
                            yield data

                            # Handle events/errors
                            error = data.get("error")
                            if error:
                                click.secho(f"\nError: {error}", fg="red")

                        except json.JSONDecodeError:
                            pass
            except Exception as e:
                click.secho(f"\nStream error: {e}", fg="red")


def _extract_content(chunk: dict) -> tuple[str, str]:
    """从 OpenAI 流式响应中提取内容 (包含 reasoning_content)"""
    event_name = str(chunk.get("_event") or "")
    if event_name == "response.output_text.delta":
        return str(chunk.get("delta") or ""), ""
    if event_name == "response.reasoning.delta":
        return "", str(chunk.get("delta") or "")
    if event_name == "response.completed":
        return "", ""

    # OpenAI 格式: {"choices": [{"delta": {"content": "xxx", "reasoning_content": "thought"}}]}
    try:
        choices = chunk.get("choices", [])
        if choices:
            delta = choices[0].get("delta", {})
            return delta.get("content", "") or "", delta.get("reasoning_content", "") or ""
    except (KeyError, IndexError):
        pass

    if isinstance(chunk.get("output_text"), str):
        return chunk["output_text"], ""
    if isinstance(chunk.get("delta"), str):
        return chunk["delta"], ""
    return "", ""


def _extract_response_content(response: dict) -> str:
    """从 OpenAI 非流式响应中提取内容"""
    output_text = response.get("output_text")
    if output_text:
        return str(output_text)
    output = response.get("output") or []
    for item in output:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("text"):
                return str(content["text"])

    # OpenAI 格式: {"choices": [{"message": {"content": "xxx"}}]}
    try:
        choices = response.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
            return message.get("content", "")
    except (KeyError, IndexError):
        pass
    return str(response)
