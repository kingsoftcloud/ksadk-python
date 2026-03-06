"""agentengine dashboard - 打开云端已部署 Agent 的 Web UI。"""

from __future__ import annotations

import asyncio
import webbrowser
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import click

from ksadk.api import AgentEngineClient
from ksadk.cli.agent_ref import ResolvedAgentRef, merge_agent_inputs, resolve_agent_ref
from ksadk.cli.ui import print_error, print_info, print_kv, print_success, print_warn
from ksadk.deployment.state import load_state


@click.command()
@click.argument("agent_ref", required=False)
@click.option("--agent", "--agent-id", "agent_option", "-a", help="Agent 名称或 ID")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--path", "ui_path", default="/", show_default=True, help="云端 Dashboard UI 路径")
@click.option("--direct", is_flag=True, help="直接打开 endpoint（跳过 UI Ticket）")
@click.option(
    "--ticket-expires-seconds",
    default=3600,
    show_default=True,
    type=click.IntRange(30, 3600),
    help="Dashboard UI ticket 有效期（秒）",
)
def dashboard(
    agent_ref: Optional[str],
    agent_option: Optional[str],
    region: str,
    ui_path: str,
    direct: bool,
    ticket_expires_seconds: int,
):
    """在浏览器中打开云端已部署 Agent 的 Dashboard/WebUI。"""
    try:
        explicit_ref = merge_agent_inputs(
            agent_option=agent_option,
            positional_agent=agent_ref,
        )
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

    try:
        detail, used_ref, state_stale = asyncio.run(
            _resolve_agent_detail(region, primary_ref, fallback_ref)
        )
    except Exception as e:
        print_error(f"获取 Agent 信息失败: {e}")
        raise SystemExit(1)

    if state_stale:
        print_warn(".agentengine.state 指向的 Agent 不存在，已自动回退到项目配置")
    if used_ref.source != primary_ref.source:
        print_info(f"当前使用 {used_ref.source_text}: {used_ref.value}")

    endpoint = (detail.get("endpoint") or "").strip()
    if not endpoint:
        print_error("目标 Agent 未返回 Endpoint，暂时无法打开 Dashboard")
        raise SystemExit(1)

    is_openclaw = (detail.get("framework") or "").strip().lower() == "openclaw"
    gateway_token = _resolve_gateway_token(state, detail)
    if gateway_token:
        is_openclaw = True

    base_url = _build_base_ui_url(endpoint, ui_path)

    if direct:
        open_url = base_url
        if is_openclaw and gateway_token:
            open_url = _set_fragment_token(open_url, gateway_token)
        print_success("打开 Dashboard")
        print_kv("URL", open_url, value_style="#58a6ff")
        webbrowser.open(open_url)
        return

    try:
        ticket_data = asyncio.run(
            _create_dashboard_ticket(
                region=region,
                agent_id=(detail.get("agent_id") or "").strip() or None,
                agent_name=(detail.get("name") or "").strip() or None,
                expires_seconds=ticket_expires_seconds,
            )
        )
    except Exception as e:
        if _is_not_found_error(e):
            print_error("CreateDashboardTicket 不可用（404）")
            print_info("请升级服务端后重试；当前版本不再回退 API Key URL 票据")
        else:
            print_error(f"创建 UI Ticket 失败: {e}")
        raise SystemExit(1)

    ui_ticket = (ticket_data.get("ticket") or "").strip()
    if not ui_ticket:
        print_error("CreateDashboardTicket 返回为空")
        raise SystemExit(1)

    query_params = {"ae_ui_ticket": ui_ticket}
    if is_openclaw:
        query_params["gatewayUrl"] = _build_ws_gateway_url(endpoint)
        if gateway_token:
            query_params["token"] = gateway_token

    open_url = _append_query_params(base_url, query_params)
    if is_openclaw and gateway_token:
        open_url = _set_fragment_token(open_url, gateway_token)

    print_success("已创建短时效 UI Ticket，打开 Dashboard")
    print_kv("URL", open_url, value_style="#58a6ff")
    webbrowser.open(open_url)


def _resolve_references(
    explicit_ref: Optional[str],
    cwd: Path,
) -> Tuple[Optional[ResolvedAgentRef], Optional[ResolvedAgentRef]]:
    """解析 primary/fallback 引用。"""
    if explicit_ref:
        return (
            ResolvedAgentRef(value=explicit_ref, source="cli"),
            None,
        )

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
    """解析 Agent 详情；state 失效时可回退配置。"""
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
    """按 ID/Name 两种方式尝试查询 Agent 详情。"""
    err: Optional[Exception] = None
    attempts = (
        {"agent_id": agent_ref},
        {"name": agent_ref},
    )
    for kwargs in attempts:
        try:
            agent = await client.get_agent(**kwargs)
            if agent:
                detail = _flatten_agent_detail(agent)
                if detail.get("agent_id") or detail.get("endpoint"):
                    return detail, None
        except Exception as e:
            err = e
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


def _flatten_agent_detail(agent: dict) -> dict:
    """将 GetAgent 响应转换为扁平结构，兼容新旧字段。"""
    basic = agent.get("basic", {}) if isinstance(agent, dict) else {}
    quick = agent.get("quick_access", {}) if isinstance(agent, dict) else {}
    deploy = agent.get("deployment", {}) if isinstance(agent, dict) else {}

    return {
        "agent_id": basic.get("agent_id") or agent.get("agent_id") or "",
        "name": basic.get("name") or agent.get("name") or "",
        "framework": deploy.get("framework") or basic.get("framework") or agent.get("framework") or "",
        "endpoint": quick.get("public_endpoint") or quick.get("private_endpoint") or agent.get("endpoint") or "",
    }


def _resolve_gateway_token(state: dict, detail: dict) -> Optional[str]:
    if not state or state.get("type") != "openclaw":
        return None

    token = (state.get("gateway_token") or "").strip()
    if not token:
        return None

    detail_id = (detail.get("agent_id") or "").strip()
    detail_name = (detail.get("name") or "").strip()
    state_id = (state.get("agent_id") or "").strip()
    state_name = (state.get("name") or "").strip()

    if detail_id and state_id and detail_id == state_id:
        return token
    if detail_name and state_name and detail_name == state_name:
        return token
    return None


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


def _set_fragment_token(url: str, token: str) -> str:
    parts = urlsplit(url)
    fragment = f"token={token}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, fragment))


def _build_ws_gateway_url(endpoint: str) -> str:
    parsed = urlsplit(endpoint.rstrip("/"))
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{ws_scheme}://{parsed.netloc}/"


def _is_not_found_error(err: Exception) -> bool:
    text = str(err).lower()
    return "code: 404" in text or "not found" in text
