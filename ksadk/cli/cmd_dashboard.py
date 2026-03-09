"""agentengine dashboard - 打开云端已部署 Agent UI（短链接模式）。"""

from __future__ import annotations

import asyncio
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import click

from ksadk.api import AgentEngineClient
from ksadk.cli.agent_ref import ResolvedAgentRef, merge_agent_inputs, resolve_agent_ref
from ksadk.cli.ui import get_console, new_table, print_error, print_info, print_kv, print_success, print_warn
from ksadk.deployment.state import load_state
from ksadk.deployment.ui_config import resolve_ui_config

console = get_console()


class DashboardGroup(click.Group):
    """支持 `agentengine dashboard [agent_ref]` + 子命令共存。"""

    def parse_args(self, ctx, args):
        ctx.ensure_object(dict)
        if args:
            first = args[0]
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


@click.group("dashboard", cls=DashboardGroup, invoke_without_command=True)
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
@click.option("--no-open", is_flag=True, help="仅打印 URL，不自动打开浏览器")
@click.option("--direct", is_flag=True, help="直接打开 endpoint/path（跳过短链接创建）")
@click.option("--legacy-ticket", is_flag=True, help="使用旧版 CreateDashboardTicket（排障用途）")
@click.option(
    "--ticket-expires-seconds",
    default=3600,
    show_default=True,
    type=click.IntRange(30, 3600),
    help="旧版 ticket 有效期（仅 --legacy-ticket 生效）",
)
@click.pass_context
def dashboard(
    ctx: click.Context,
    agent_option: Optional[str],
    region: str,
    ui_path: Optional[str],
    share: bool,
    expires_seconds: Optional[int],
    no_open: bool,
    direct: bool,
    legacy_ticket: bool,
    ticket_expires_seconds: int,
):
    """统一打开云端 Dashboard（默认短链接模式）。"""
    if ctx.invoked_subcommand is not None:
        return

    positional_ref = None
    if isinstance(ctx.obj, dict):
        positional_ref = ctx.obj.get("positional_agent_ref")

    _open_dashboard(
        positional_agent=positional_ref,
        agent_option=agent_option,
        region=region,
        ui_path=ui_path,
        share=share,
        expires_seconds=expires_seconds,
        no_open=no_open,
        direct=direct,
        legacy_ticket=legacy_ticket,
        ticket_expires_seconds=ticket_expires_seconds,
    )


@dashboard.group("share")
def dashboard_share():
    """Dashboard 分享链接管理。"""


@dashboard_share.command("list")
@click.argument("agent_ref", required=False)
@click.option("--agent", "--agent-id", "agent_option", "-a", help="Agent 名称或 ID")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--type", "link_type", type=click.Choice(["private", "share"]), default=None, help="链接类型过滤")
@click.option("--status", type=click.Choice(["active", "revoked"]), default=None, help="状态过滤")
@click.option("--page", default=1, show_default=True, type=click.IntRange(1, 10_000))
@click.option("--size", default=20, show_default=True, type=click.IntRange(1, 100))
def dashboard_share_list(
    agent_ref: Optional[str],
    agent_option: Optional[str],
    region: str,
    link_type: Optional[str],
    status: Optional[str],
    page: int,
    size: int,
):
    """列出 Agent 的 Dashboard 分享链接。"""
    try:
        explicit_ref = merge_agent_inputs(agent_option=agent_option, positional_agent=agent_ref)
    except ValueError as e:
        print_error(str(e))
        raise SystemExit(1)

    cwd = Path(".").resolve()
    state = load_state(cwd)
    primary_ref, fallback_ref = _resolve_references(explicit_ref, cwd)
    if not primary_ref:
        print_error("未找到可用 Agent，请指定 Agent（--agent 或位置参数）")
        raise SystemExit(1)

    try:
        detail, _, _ = asyncio.run(_resolve_agent_detail(region, primary_ref, fallback_ref))
    except Exception as e:
        print_error(f"获取 Agent 信息失败: {e}")
        raise SystemExit(1)
    agent_id = (detail.get("agent_id") or "").strip()
    agent_name = (detail.get("name") or "").strip()
    if not agent_id and not agent_name:
        print_error("无法解析 Agent 标识")
        raise SystemExit(1)

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
        print_error(f"查询 Dashboard 链接失败: {e}")
        raise SystemExit(1)
    links = result.get("links") or []
    total = int(result.get("total") or len(links))
    if not links:
        print_info("没有找到匹配的 Dashboard 链接")
        return

    table = new_table(f"Dashboard 链接 [muted](总计: {total})[/]")
    table.add_column("LinkId", style="#58a6ff", no_wrap=True)
    table.add_column("Type", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Path", no_wrap=True)
    table.add_column("ExpiresAt", overflow="fold")
    table.add_column("CreatedAt", overflow="fold")

    for item in links:
        table.add_row(
            str(item.get("link_id") or "-"),
            str(item.get("link_type") or "-"),
            str(item.get("status") or "-"),
            str(item.get("path") or "/"),
            _format_dashboard_time(item.get("expires_at"), never_text="永久"),
            _format_dashboard_time(item.get("created_at"), never_text="-"),
        )
    console.print(table)
    if state and state.get("agent_id"):
        print_kv("当前状态文件 Agent", str(state.get("agent_id")))


@dashboard_share.command("revoke")
@click.argument("link_id", required=True)
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
def dashboard_share_revoke(link_id: str, region: str):
    """撤销 Dashboard 分享链接。"""
    try:
        result = asyncio.run(_delete_dashboard_access_link(region=region, link_id=link_id.strip()))
    except Exception as e:
        print_error(f"撤销失败: {e}")
        raise SystemExit(1)
    deleted = bool(result.get("deleted", False))
    if deleted:
        print_success("链接已删除")
        print_kv("LinkId", link_id)
    else:
        print_warn("接口未返回 Deleted=true，请检查服务端日志")


def _open_dashboard(
    *,
    positional_agent: Optional[str],
    agent_option: Optional[str],
    region: str,
    ui_path: Optional[str],
    share: bool,
    expires_seconds: Optional[int],
    no_open: bool,
    direct: bool,
    legacy_ticket: bool,
    ticket_expires_seconds: int,
):
    try:
        explicit_ref = merge_agent_inputs(agent_option=agent_option, positional_agent=positional_agent)
    except ValueError as e:
        print_error(str(e))
        raise SystemExit(1)

    cwd = Path(".").resolve()
    state = load_state(cwd)
    primary_ref, fallback_ref = _resolve_references(explicit_ref, cwd)
    if not primary_ref:
        print_error("未找到可用 Agent，请指定 Agent（--agent 或位置参数）")
        print_info("自动解析顺序: .agentengine.state -> agentengine.yaml/ksadk.yaml")
        raise SystemExit(1)

    if primary_ref.source != "cli":
        print_info(f"未显式指定 Agent，使用 {primary_ref.source_text}: {primary_ref.value}")

    detail, used_ref, state_stale = asyncio.run(_resolve_agent_detail(region, primary_ref, fallback_ref))
    if state_stale:
        print_warn(".agentengine.state 指向的 Agent 不存在，已自动回退到项目配置")
    if used_ref.source != primary_ref.source:
        print_info(f"当前使用 {used_ref.source_text}: {used_ref.value}")

    endpoint = (detail.get("endpoint") or "").strip()
    if not endpoint:
        print_error("目标 Agent 未返回 Endpoint，无法打开 Dashboard")
        raise SystemExit(1)

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

    if legacy_ticket:
        ticket_data = asyncio.run(
            _create_dashboard_ticket(
                region=region,
                agent_id=(detail.get("agent_id") or "").strip() or None,
                agent_name=(detail.get("name") or "").strip() or None,
                expires_seconds=ticket_expires_seconds,
            )
        )
        ui_ticket = (ticket_data.get("ticket") or "").strip()
        if not ui_ticket:
            print_error("CreateDashboardTicket 返回为空")
            raise SystemExit(1)
        open_url = _append_query_params(base_url, {"ae_ui_ticket": ui_ticket})
        _emit_url("已创建旧版 UI Ticket，打开 Dashboard", open_url, no_open=no_open)
        return

    link_type = "share" if share else "private"
    validated_expires = _normalize_expires_seconds(link_type=link_type, expires_seconds=expires_seconds)
    link_data = asyncio.run(
        _create_dashboard_access_link(
            region=region,
            agent_id=(detail.get("agent_id") or "").strip() or None,
            agent_name=(detail.get("name") or "").strip() or None,
            link_type=link_type,
            path=normalized_path,
            expires_seconds=validated_expires,
        )
    )
    access_url = (link_data.get("access_url") or "").strip()
    if not access_url:
        print_error("CreateDashboardAccessLink 返回为空")
        raise SystemExit(1)
    open_url = access_url

    print_success("已创建 Dashboard 短链接")
    print_kv("LinkId", str(link_data.get("link_id") or "-"))
    print_kv("Type", link_type)
    print_kv("ExpiresAt", _format_dashboard_time(link_data.get("expires_at"), never_text="server-default"))
    _emit_url("打开 Dashboard", open_url, no_open=no_open)


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
        if seconds < 30 or seconds > 3600:
            raise click.BadParameter("private 链接 expires-seconds 必须在 30~3600")
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

    state_ref = resolve_agent_ref(
        None,
        cwd=cwd,
        include_state=True,
        include_project_config=False,
    )
    config_ref = resolve_agent_ref(
        None,
        cwd=cwd,
        include_state=False,
        include_project_config=True,
    )
    if state_ref:
        fallback = None
        if config_ref and config_ref.value != state_ref.value:
            fallback = config_ref
        return state_ref, fallback
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


async def _create_dashboard_ticket(
    *,
    region: str,
    agent_id: Optional[str],
    agent_name: Optional[str],
    expires_seconds: int,
) -> dict:
    async with AgentEngineClient(region=region) as client:
        kwargs = {"expires_seconds": int(expires_seconds)}
        if agent_id:
            kwargs["agent_id"] = agent_id
        elif agent_name:
            kwargs["name"] = agent_name
        else:
            raise Exception("missing agent reference")
        return await client.create_dashboard_ticket(**kwargs)


async def _create_dashboard_access_link(
    *,
    region: str,
    agent_id: Optional[str],
    agent_name: Optional[str],
    link_type: str,
    path: str,
    expires_seconds: Optional[int],
) -> dict:
    async with AgentEngineClient(region=region) as client:
        kwargs = {
            "link_type": link_type,
            "path": path,
            "expires_seconds": expires_seconds,
        }
        if agent_id:
            kwargs["agent_id"] = agent_id
        elif agent_name:
            kwargs["name"] = agent_name
        else:
            raise Exception("missing agent reference")
        return await client.create_dashboard_access_link(**kwargs)


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


async def _delete_dashboard_access_link(*, region: str, link_id: str) -> dict:
    async with AgentEngineClient(region=region) as client:
        return await client.delete_dashboard_access_link(link_id=link_id)


def _flatten_agent_detail(agent: dict) -> dict:
    basic = agent.get("basic", {}) if isinstance(agent, dict) else {}
    quick = agent.get("quick_access", {}) if isinstance(agent, dict) else {}
    deploy = agent.get("deployment", {}) if isinstance(agent, dict) else {}
    return {
        "agent_id": basic.get("agent_id") or agent.get("agent_id") or "",
        "name": basic.get("name") or agent.get("name") or "",
        "framework": deploy.get("framework") or basic.get("framework") or agent.get("framework") or "",
        "endpoint": quick.get("public_endpoint") or quick.get("private_endpoint") or agent.get("endpoint") or "",
    }


def _normalize_ui_path(path: str) -> str:
    normalized = (path or "/").strip() or "/"
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized


def _build_base_ui_url(endpoint: str, ui_path: str) -> str:
    return f"{endpoint.rstrip('/')}{_normalize_ui_path(ui_path)}"


def _append_query_params(url: str, params: dict) -> str:
    parts = urlsplit(url)
    query_items = parse_qsl(parts.query, keep_blank_values=True)
    for k, v in params.items():
        if v is None:
            continue
        query_items.append((k, str(v)))
    new_query = urlencode(query_items, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def _is_not_found_error(err: Optional[Exception]) -> bool:
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
