"""agentengine dashboard - Dashboard 命令组（canonical: `dashboard open`）。"""

from __future__ import annotations

import asyncio
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

import click

from ksadk.api import AgentEngineAPIError, AgentEngineClient
from ksadk.cli.agent_ref import (
    ResolvedAgentRef,
    merge_agent_inputs,
    resolve_agent_ref,
    resolve_openclaw_ref,
)
from ksadk.cli.dry_run import dry_run_option, effective_dry_run, run_async_with_dry_run
from ksadk.cli.error_utils import abort_with_cli_error, remote_error, resolution_error, usage_error
from ksadk.cli.resource_common import (
    CONTEXT_SETTINGS,
    ResourceActionSet,
    ResourceDescriptor,
    ResourceListSchema,
    ResourceStatusSchema,
    build_resource_group_help,
    confirm_destructive,
    confirm_options,
    pagination_options,
    print_compatibility_hint,
    render_descriptor_list,
    render_descriptor_status,
)
from ksadk.cli.ui import print_info, print_kv, print_success, print_warn
from ksadk.cli.ui import is_json_output, is_stdout_tty, output_option as cli_output_option
from ksadk.deployment.state import load_state
from ksadk.deployment.ui_config import resolve_ui_config
from ksadk.openclaw_gateway import OpenClawGatewayClient

_PATH_EMBEDDED_OPTION_HINTS = (
    "--share",
    "--expires-seconds",
    "--force-new",
    "--no-open",
    "--direct",
    "--output",
    "--agent",
    "--agent-id",
    "--region",
)

DEFAULT_PRIVATE_LINK_EXPIRES_SECONDS = 24 * 60 * 60

DASHBOARD_RESOURCE = ResourceDescriptor(
    name="Dashboard",
    summary="Dashboard 资源管理。",
    resource_key="dashboard",
    actions=ResourceActionSet(
        open="agentengine dashboard open [agent_ref]",
        extra=("share",),
    ),
    examples=(
        "agentengine dashboard open",
        "agentengine dashboard open ar-xxxx",
        "agentengine dashboard share list ar-xxxx",
    ),
    missing_ref_message="未找到可用 Agent，请指定 Agent（--agent 或位置参数）",
    resolution_commands=(
        "agentengine agent list",
        "agentengine dashboard open --agent <AgentName|AgentId>",
    ),
    open_action_help="打开 Agent Dashboard",
    extra_action_help=(
        ("share", "管理 Dashboard 分享链接"),
    ),
)

DASHBOARD_SHARE_RESOURCE = ResourceDescriptor(
    name="Dashboard 链接",
    summary="Dashboard 分享链接管理。",
    resource_key="dashboard_share",
    actions=ResourceActionSet(
        list="agentengine dashboard share list [agent_ref]",
        delete="agentengine dashboard share revoke <link_id>",
    ),
    list_schema=ResourceListSchema(
        title="Dashboard 链接列表",
        noun="Dashboard 链接",
        columns=(
            {"header": "ID", "key": "id", "style": "#58a6ff", "no_wrap": True},
            {"header": "类型", "key": "type", "no_wrap": True},
            {"header": "状态", "key": "status", "no_wrap": True},
            {"header": "路径", "key": "path", "no_wrap": True},
            {"header": "过期时间", "key": "expires_at", "overflow": "fold"},
            {"header": "创建时间", "key": "created_at", "overflow": "fold"},
        ),
        empty_message="没有找到匹配的 Dashboard 链接",
        summary_lines=("使用 `agentengine dashboard share revoke <link_id>` 撤销链接。",),
    ),
    status_schema=ResourceStatusSchema(
        title="Dashboard 链接结果",
        next_steps=("agentengine dashboard share list --agent <AgentName|AgentId>",),
    ),
    missing_ref_message="未找到可用 Agent，请指定 Agent（--agent 或位置参数）",
    resolution_commands=(
        "agentengine agent list",
        "agentengine dashboard open --agent <AgentName|AgentId>",
    ),
    list_action_help="列出 Dashboard 分享链接",
    delete_action_help="撤销 Dashboard 分享链接",
)

class DashboardGroup(click.Group):
    """支持 `dashboard open` canonical + `dashboard [agent_ref]` 兼容路径。"""

    def parse_args(self, ctx, args):
        ctx.ensure_object(dict)
        if args:
            first = args[0]
            if first == "list":
                raise click.UsageError(
                    "不支持 `agentengine dashboard list`。打开 Dashboard 请使用 "
                    "`agentengine dashboard open [agent_ref]`；查看分享链接请使用 "
                    "`agentengine dashboard share list`。"
                )
            if not first.startswith("-") and first not in self.commands:
                ctx.obj["positional_agent_ref"] = first
                args = args[1:]
        return super().parse_args(ctx, args)


def _parse_expires_seconds_option(
    _ctx: click.Context,
    _param: click.Parameter,
    value: Optional[str],
) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text in {"never", "permanent", "forever"}:
        return 0
    try:
        return int(text)
    except ValueError as e:
        raise click.BadParameter("必须是整数秒，或 never") from e


def _abort_dashboard_error(
    err: Exception,
    *,
    context: str | None = None,
    argv: list[str] | None = None,
    show_help: bool = False,
) -> None:
    abort_with_cli_error(err, context=context, argv=argv, show_help=show_help)


@click.group(
    "dashboard",
    cls=DashboardGroup,
    invoke_without_command=True,
    context_settings=CONTEXT_SETTINGS,
    help=build_resource_group_help(DASHBOARD_RESOURCE),
)
@click.option("--agent", "--agent-id", "agent_option", "-a", hidden=True, help="(兼容) Agent 名称或 ID")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", hidden=True, help="(兼容) 区域")
@click.option("--path", "ui_path", default=None, hidden=True, help="(兼容) 目标 UI 路径")
@click.option("--share", is_flag=True, hidden=True, help="(兼容) 创建可分享链接")
@click.option(
    "--expires-seconds",
    default=None,
    type=str,
    callback=_parse_expires_seconds_option,
    hidden=True,
    help="(兼容) 链接有效期（秒）；支持 never(=0)",
)
@click.option("--force-new", is_flag=True, hidden=True, help="(兼容) 强制新建链接（跳过复用）")
@click.option("--no-open", is_flag=True, hidden=True, help="(兼容) 仅打印 URL，不自动打开浏览器")
@click.option("--direct", is_flag=True, hidden=True, help="(兼容) 直接打开 endpoint/path")
@cli_output_option(hidden=True)
@click.pass_context
def dashboard(
    ctx: click.Context,
    agent_option: Optional[str],
    region: str,
    ui_path: Optional[str],
    share: bool,
    expires_seconds: Optional[int],
    force_new: bool,
    no_open: bool,
    direct: bool,
    output_mode: str | None,
):
    _ = output_mode
    if ctx.invoked_subcommand is not None:
        return

    positional_ref = None
    if isinstance(ctx.obj, dict):
        positional_ref = ctx.obj.get("positional_agent_ref")

    print_compatibility_hint(
        legacy="agentengine dashboard",
        canonical="agentengine dashboard open",
    )
    _open_dashboard(
        positional_agent=positional_ref,
        agent_option=agent_option,
        region=region,
        ui_path=ui_path,
        share=share,
        expires_seconds=expires_seconds,
        force_new=force_new,
        no_open=no_open,
        direct=direct,
    )


@dashboard.command("open", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@click.option("--agent", "--agent-id", "agent_option", "-a", help="Agent 名称或 ID")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--path", "ui_path", default=None, help="目标 UI 路径（默认根据配置自动推导）")
@click.option("--share", is_flag=True, help="创建可分享链接（默认创建私有临时链接）")
@click.option(
    "--expires-seconds",
    default=None,
    type=str,
    callback=_parse_expires_seconds_option,
    help="链接有效期（秒）；支持 never(=0)",
)
@click.option("--force-new", is_flag=True, help="强制新建链接（跳过复用）")
@click.option("--no-open", is_flag=True, help="仅打印 URL，不自动打开浏览器")
@click.option("--direct", is_flag=True, help="直接打开 endpoint/path（跳过短链接创建）")
@cli_output_option()
def dashboard_open(
    agent_ref: Optional[str],
    agent_option: Optional[str],
    region: str,
    ui_path: Optional[str],
    share: bool,
    expires_seconds: Optional[int],
    force_new: bool,
    no_open: bool,
    direct: bool,
    output_mode: str | None,
):
    """打开 Agent Dashboard。"""
    _ = output_mode
    _open_dashboard(
        positional_agent=agent_ref,
        agent_option=agent_option,
        region=region,
        ui_path=ui_path,
        share=share,
        expires_seconds=expires_seconds,
        force_new=force_new,
        no_open=no_open,
        direct=direct,
    )


@dashboard.group("share", context_settings=CONTEXT_SETTINGS, help=build_resource_group_help(DASHBOARD_SHARE_RESOURCE))
def dashboard_share():
    pass


@dashboard_share.command("list", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@click.option("--agent", "--agent-id", "agent_option", "-a", help="Agent 名称或 ID")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--type", "link_type", type=click.Choice(["private", "share"]), default=None, help="链接类型过滤")
@click.option("--status", type=click.Choice(["active", "revoked"]), default=None, help="状态过滤")
@pagination_options(default_page=1, default_size=20)
@cli_output_option()
def dashboard_share_list(
    agent_ref: Optional[str],
    agent_option: Optional[str],
    region: str,
    link_type: Optional[str],
    status: Optional[str],
    page: int,
    size: int,
    output_mode: str | None,
):
    """列出 Agent 的 Dashboard 分享链接。"""
    _ = output_mode
    try:
        explicit_ref = merge_agent_inputs(agent_option=agent_option, positional_agent=agent_ref)
    except ValueError as e:
        _abort_dashboard_error(
            usage_error(str(e)),
            argv=["dashboard", "share", "list"],
        )

    cwd = Path(".").resolve()
    state = load_state(cwd)
    primary_ref, fallback_ref = _resolve_references(explicit_ref, cwd)
    if not primary_ref:
        _abort_dashboard_error(
            resolution_error(
                DASHBOARD_SHARE_RESOURCE.missing_ref_message or "未找到可用 Agent。",
                hints=list(DASHBOARD_SHARE_RESOURCE.resolution_commands),
            ),
            argv=["dashboard", "share", "list"],
        )

    try:
        detail, _, _ = asyncio.run(_resolve_agent_detail(region, primary_ref, fallback_ref))
    except Exception as e:
        _abort_dashboard_error(e, context="获取 Agent 信息失败", argv=["dashboard", "share", "list"])
    agent_id = (detail.get("agent_id") or "").strip()
    agent_name = (detail.get("name") or "").strip()
    if not agent_id and not agent_name:
        _abort_dashboard_error(
            resolution_error("无法解析 Agent 标识", hints=list(DASHBOARD_SHARE_RESOURCE.resolution_commands)),
            argv=["dashboard", "share", "list"],
        )

    try:
        result = asyncio.run(
            _list_dashboard_access_links(
                region=region,
                agent_id=agent_id or None,
                agent_name=agent_name or None,
                link_type=link_type,
                status=status,
                page=page,
                size=size,
            )
        )
    except Exception as e:
        _abort_dashboard_error(e, context="查询 Dashboard 链接失败", argv=["dashboard", "share", "list"])
    links = result.get("links") or []
    total = int(result.get("total") or len(links))
    render_descriptor_list(
        DASHBOARD_SHARE_RESOURCE,
        rows=[
            (
                str(item.get("link_id") or "-"),
                str(item.get("link_type") or "-"),
                str(item.get("status") or "-"),
                str(item.get("path") or "/"),
                _format_dashboard_time(item.get("expires_at"), never_text="永久"),
                _format_dashboard_time(item.get("created_at"), never_text="-"),
            )
            for item in links
        ],
        total=total,
        page=page,
        size=size,
    )
    if state and state.get("agent_id"):
        print_kv("当前状态文件 Agent", str(state.get("agent_id")))


@dashboard_share.command("revoke", context_settings=CONTEXT_SETTINGS)
@click.argument("link_id", required=True)
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@confirm_options()
@dry_run_option()
@cli_output_option()
def dashboard_share_revoke(
    link_id: str,
    region: str,
    assume_yes: bool,
    dry_run: bool,
    output_mode: str | None,
):
    """撤销 Dashboard 分享链接。"""
    _ = output_mode
    dry_run = effective_dry_run(dry_run)
    if not confirm_destructive(
        assume_yes=assume_yes,
        dry_run=dry_run,
        prompt=f"确定要撤销 Dashboard 分享链接 `{link_id}` 吗?",
    ):
        return
    try:
        result = run_async_with_dry_run(
            _delete_dashboard_access_link(region=region, link_id=link_id.strip(), dry_run=dry_run),
            dry_run=dry_run,
            dry_run_resource="dashboard_share",
            dry_run_action="revoke",
        )
    except Exception as e:
        _abort_dashboard_error(e, context="撤销失败", argv=["dashboard", "share", "revoke"])
        return
    if result is None:
        return
    deleted = bool(result.get("deleted", False))
    if deleted:
        render_descriptor_status(
            DASHBOARD_SHARE_RESOURCE,
            title="Dashboard 链接撤销结果",
            subtitle=link_id,
            fields=[("ID", link_id, "#58a6ff"), ("状态", "已撤销", "ok")],
            action="revoke",
            item={"id": link_id, "status": "revoked", "deleted": True},
        )
    else:
        _abort_dashboard_error(
            remote_error(
                "接口未确认撤销成功。",
                details={"id": link_id, "deleted": False},
            ),
            argv=["dashboard", "share", "revoke"],
        )


def _open_dashboard(
    *,
    positional_agent: Optional[str],
    agent_option: Optional[str],
    region: str,
    ui_path: Optional[str],
    share: bool,
    expires_seconds: Optional[int],
    force_new: bool,
    no_open: bool,
    direct: bool,
):
    no_open = bool(no_open or is_json_output() or not is_stdout_tty())
    try:
        _validate_ui_path_option(ui_path)
    except ValueError as e:
        _abort_dashboard_error(usage_error(str(e)), argv=["dashboard", "open"])
        return

    try:
        explicit_ref = merge_agent_inputs(agent_option=agent_option, positional_agent=positional_agent)
    except ValueError as e:
        _abort_dashboard_error(usage_error(str(e)), argv=["dashboard", "open"])

    cwd = Path(".").resolve()
    state = load_state(cwd)
    primary_ref, fallback_ref = _resolve_references(explicit_ref, cwd)
    if not primary_ref:
        _abort_dashboard_error(
            resolution_error(
                DASHBOARD_RESOURCE.missing_ref_message or "未找到可用 Agent。",
                hints=list(DASHBOARD_RESOURCE.resolution_commands),
            ),
            argv=["dashboard", "open"],
        )

    if primary_ref.source != "cli":
        print_info(f"未显式指定 Agent，使用 {primary_ref.source_text}: {primary_ref.value}")

    try:
        detail, used_ref, state_stale = asyncio.run(_resolve_agent_detail(region, primary_ref, fallback_ref))
    except Exception as e:
        _abort_dashboard_error(e, context="获取 Agent 信息失败", argv=["dashboard", "open"])
        return
    if state_stale:
        print_warn(".agentengine.state 指向的 Agent 不存在，已自动回退到项目配置")
    if used_ref.source != primary_ref.source:
        print_info(f"当前使用 {used_ref.source_text}: {used_ref.value}")

    endpoint = (detail.get("endpoint") or "").strip()
    if not endpoint:
        _abort_dashboard_error(
            resolution_error("目标 Agent 未返回 Endpoint，无法打开 Dashboard"),
            argv=["dashboard", "open"],
        )

    resolved_ui = resolve_ui_config(
        framework=(detail.get("framework") or "").strip(),
        state=state,
        cli_profile=None,
        cli_path=ui_path,
        cli_url=None,
    )
    normalized_path = _normalize_ui_path(resolved_ui.path or "/")
    base_url = _build_base_ui_url(endpoint, normalized_path)

    if direct:
        _emit_url("打开 Dashboard（direct）", base_url, no_open=no_open)
        return

    if _is_openclaw_target(state=state, detail=detail):
        try:
            link_data = asyncio.run(
                _create_openclaw_gateway_access_link(
                    region=region,
                    detail=detail,
                    path=normalized_path,
                    link_type="share" if share else "private",
                    expires_seconds=_normalize_expires_seconds(
                        link_type="share" if share else "private",
                        expires_seconds=expires_seconds,
                    ),
                    force_new=force_new,
                )
            )
        except Exception as e:
            _abort_dashboard_error(e, context="创建 OpenClaw gateway 链接失败", argv=["dashboard", "open"])
            return
        _render_dashboard_open_result(detail=detail, link_data=link_data, no_open=no_open)
        return

    link_type = "share" if share else "private"
    validated_expires = _normalize_expires_seconds(link_type=link_type, expires_seconds=expires_seconds)
    try:
        link_data = asyncio.run(
                _create_dashboard_access_link(
                    region=region,
                    agent_id=(detail.get("agent_id") or "").strip() or None,
                    agent_name=(detail.get("name") or "").strip() or None,
                    link_type=link_type,
                    path=normalized_path,
                    expires_seconds=validated_expires,
                    force_new=force_new,
                )
            )
    except Exception as e:
        _abort_dashboard_error(e, context="创建 Dashboard 链接失败", argv=["dashboard", "open"])
        return
    access_url = (link_data.get("access_url") or "").strip()
    if not access_url:
        _abort_dashboard_error(
            remote_error("CreateDashboardAccessLink 返回为空"),
            argv=["dashboard", "open"],
        )
    _render_dashboard_open_result(detail=detail, link_data=link_data, no_open=no_open, default_link_type=link_type)


def _render_dashboard_open_result(
    *,
    detail: dict,
    link_data: dict,
    no_open: bool,
    default_link_type: str = "private",
):
    open_url = (link_data.get("access_url") or "").strip()
    actual_link_type = str(link_data.get("link_type") or default_link_type or "").strip() or default_link_type

    render_descriptor_status(
        DASHBOARD_SHARE_RESOURCE,
        title="Dashboard 打开结果",
        subtitle=str(detail.get("name") or detail.get("agent_id") or "-"),
        fields=[
            ("ID", str(link_data.get("link_id") or "-"), "#58a6ff"),
            ("类型", actual_link_type, None),
            ("过期时间", _format_dashboard_time(link_data.get("expires_at"), never_text="server-default"), None),
        ],
        action="open",
        item={
            "link_id": str(link_data.get("link_id") or "-"),
            "type": actual_link_type,
            "expires_at": _format_dashboard_time(link_data.get("expires_at"), never_text="server-default"),
            "url": open_url,
            "agent_id": str(detail.get("agent_id") or ""),
            "agent_name": str(detail.get("name") or ""),
        },
    )
    langfuse_url = str(detail.get("langfuse_url") or "").strip()
    if langfuse_url:
        print_kv("Langfuse", langfuse_url, value_style="#58a6ff")
    _emit_url("打开 Dashboard", open_url, no_open=no_open)


def _validate_ui_path_option(ui_path: Optional[str]) -> None:
    if not ui_path:
        return
    path_text = str(ui_path).strip()
    for option_hint in _PATH_EMBEDDED_OPTION_HINTS:
        if option_hint in path_text:
            raise ValueError(
                f"--path 的值疑似拼入了 `{option_hint}`。"
                "请把路径和选项分开，例如：`agentengine dashboard open --path /chat --share`。"
            )


def _is_openclaw_target(*, state: Optional[dict], detail: dict) -> bool:
    state_type = str((state or {}).get("type") or "").strip().lower()
    framework = str(detail.get("framework") or "").strip().lower()
    return state_type == "openclaw" or framework == "openclaw"


def _emit_url(title: str, url: str, *, no_open: bool):
    print_success(title)
    print_kv("URL", url, value_style="#58a6ff")
    if not no_open:
        webbrowser.open(url)


def _normalize_expires_seconds(*, link_type: str, expires_seconds: Optional[int]) -> Optional[int]:
    if expires_seconds is None:
        return None
    seconds = int(expires_seconds)
    if link_type == "private":
        if seconds < 30 or seconds > DEFAULT_PRIVATE_LINK_EXPIRES_SECONDS:
            raise click.BadParameter(
                f"private 链接 expires-seconds 必须在 30~{DEFAULT_PRIVATE_LINK_EXPIRES_SECONDS}"
            )
        return seconds
    if seconds == 0:
        return 0
    if seconds < 300 or seconds > 2592000:
        raise click.BadParameter("share 链接 expires-seconds 必须为 0 或 300~2592000")
    return seconds


def _resolve_references(
    explicit_ref: Optional[str],
    cwd: Path,
) -> Tuple[Optional[ResolvedAgentRef], Optional[ResolvedAgentRef]]:
    if explicit_ref:
        return ResolvedAgentRef(value=explicit_ref, source="cli"), None

    hermes_state_ref = resolve_agent_ref(None, cwd=cwd, include_state=True, include_project_config=False)
    openclaw_state_ref = resolve_openclaw_ref(
        None,
        cwd=cwd,
        include_state=True,
    )
    config_ref = resolve_agent_ref(
        None,
        cwd=cwd,
        include_state=False,
        include_project_config=True,
    )
    if hermes_state_ref:
        fallback = None
        if config_ref and config_ref.value != hermes_state_ref.value:
            fallback = config_ref
        return hermes_state_ref, fallback
    if openclaw_state_ref:
        fallback = None
        if config_ref and config_ref.value != openclaw_state_ref.value:
            fallback = config_ref
        return (
            ResolvedAgentRef(
                value=openclaw_state_ref.value,
                source=openclaw_state_ref.source,
                source_path=openclaw_state_ref.source_path,
            ),
            fallback,
        )
    return config_ref, None


async def _resolve_agent_detail(
    region: str,
    primary_ref: ResolvedAgentRef,
    fallback_ref: Optional[ResolvedAgentRef],
) -> Tuple[dict, ResolvedAgentRef, bool]:
    async with AgentEngineClient(region=region) as client:
        detail, err = await _try_get_agent_detail(client, primary_ref.value)
        if detail:
            return detail, primary_ref, False

        can_fallback = (
            primary_ref.source.startswith("state.")
            and fallback_ref is not None
            and _is_not_found_error(err)
        )
        if can_fallback:
            fallback_detail, fallback_err = await _try_get_agent_detail(client, fallback_ref.value)
            if fallback_detail:
                return fallback_detail, fallback_ref, True
            if fallback_err:
                raise fallback_err

        if err:
            raise err
        raise Exception("Agent not found")


async def _try_get_agent_detail(client: AgentEngineClient, agent_ref: str) -> Tuple[Optional[dict], Optional[Exception]]:
    err: Optional[Exception] = None
    for kwargs in ({"agent_id": agent_ref}, {"name": agent_ref}):
        try:
            agent = await client.get_agent(**kwargs)
            if agent:
                return _flatten_agent_detail(agent), None
        except Exception as e:
            err = e
            if not _is_not_found_error(e):
                return None, err
    return None, err


async def _create_dashboard_access_link(
    *,
    region: str,
    agent_id: Optional[str],
    agent_name: Optional[str],
    link_type: str,
    path: str,
    expires_seconds: Optional[int],
    force_new: bool,
) -> dict:
    async with AgentEngineClient(region=region) as client:
        kwargs = {
            "link_type": link_type,
            "path": path,
            "expires_seconds": expires_seconds,
            "force_new": force_new,
        }
        if agent_id:
            kwargs["agent_id"] = agent_id
        elif agent_name:
            kwargs["name"] = agent_name
        else:
            raise Exception("missing agent reference")
        return await client.create_dashboard_access_link(**kwargs)


def _build_openclaw_gateway_client(region: str, detail: dict) -> OpenClawGatewayClient:
    return OpenClawGatewayClient(
        region=region,
        agent_id=str(detail.get("agent_id") or "").strip(),
        agent_name=str(detail.get("name") or "").strip() or None,
    )


async def _create_openclaw_gateway_access_link(
    *,
    region: str,
    detail: dict,
    path: str = "/",
    link_type: str,
    expires_seconds: Optional[int],
    force_new: bool = False,
) -> dict:
    gateway = _build_openclaw_gateway_client(region, detail)
    try:
        info = await gateway.build_access_info(
            path=path,
            expires_seconds=expires_seconds,
            link_type=link_type,
            force_new=force_new,
        )
    finally:
        await gateway.close()
    return {
        "link_id": info.link_id,
        "link_type": link_type,
        "expires_at": info.expires_at,
        "access_url": info.access_url,
    }


async def _list_dashboard_access_links(
    *,
    region: str,
    agent_id: Optional[str],
    agent_name: Optional[str],
    link_type: Optional[str],
    status: Optional[str],
    page: int,
    size: int,
) -> dict:
    async with AgentEngineClient(region=region) as client:
        kwargs = {
            "link_type": link_type,
            "status": status,
            "page": int(page),
            "size": int(size),
        }
        if agent_id:
            kwargs["agent_id"] = agent_id
        elif agent_name:
            kwargs["name"] = agent_name
        else:
            raise Exception("missing agent reference")
        return await client.list_dashboard_access_links(**kwargs)


async def _delete_dashboard_access_link(*, region: str, link_id: str, dry_run: bool = False) -> dict:
    async with AgentEngineClient(region=region, dry_run=dry_run) as client:
        return await client.delete_dashboard_access_link(link_id=link_id)


def _flatten_agent_detail(agent: dict) -> dict:
    basic = agent.get("basic", {}) if isinstance(agent, dict) else {}
    quick = agent.get("quick_access", {}) if isinstance(agent, dict) else {}
    deploy = agent.get("deployment", {}) if isinstance(agent, dict) else {}
    adv = agent.get("advanced", {}) if isinstance(agent, dict) else {}
    return {
        "agent_id": basic.get("agent_id") or agent.get("agent_id") or "",
        "name": basic.get("name") or agent.get("name") or "",
        "framework": deploy.get("framework") or basic.get("framework") or agent.get("framework") or "",
        "endpoint": quick.get("public_endpoint") or quick.get("private_endpoint") or agent.get("endpoint") or "",
        "langfuse_url": adv.get("observability_url") or agent.get("langfuse_trace_url") or "",
    }


def _normalize_ui_path(path: str) -> str:
    normalized = (path or "/").strip() or "/"
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized


def _build_base_ui_url(endpoint: str, ui_path: str) -> str:
    return f"{endpoint.rstrip('/')}{_normalize_ui_path(ui_path)}"


def _is_not_found_error(err: Optional[Exception]) -> bool:
    if isinstance(err, AgentEngineAPIError):
        return err.code == 404 or "not found" in (err.message or "").lower()
    text = str(err or "").lower()
    return "code: 404" in text or "not found" in text


def _format_dashboard_time(value: Optional[str], *, never_text: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return never_text
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        beijing = dt.astimezone(timezone(timedelta(hours=8)))
        beijing_text = beijing.strftime("%Y-%m-%d %H:%M:%S CST")
        utc_text = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return f"{beijing_text} ({utc_text})"
    except Exception:
        return raw
