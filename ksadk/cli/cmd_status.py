"""
agentengine status - 查看 Agent 运行状态和 Endpoint

支持 watch 模式实时刷新
"""

import click
import asyncio
import time
from pathlib import Path
from datetime import datetime
from typing import Sequence
from ksadk.api.client import DryRunExit
from ksadk.cli.agent_ref import merge_agent_inputs, resolve_agent_ref
from ksadk.cli.dry_run import dry_run_option, run_async_with_dry_run, effective_dry_run
from ksadk.cli.error_utils import print_exception, resolution_error, usage_error, validation_error
from ksadk.cli.resource_common import (
    CONTEXT_SETTINGS,
    CompatibilityAliasCommand,
    print_compatibility_hint,
    render_resource_list,
    render_resource_status,
)
from ksadk.cli.ui import (
    get_console,
    is_json_output,
    print_error,
    print_info,
    print_kv,
    print_title,
    print_warn,
    status_click_color,
    status_rich_style,
    replica_rich_style,
    summary_panel,
)

console = get_console()
AGENT_LIST_HIDDEN_FRAMEWORKS = ("openclaw", "hermes")
AGENT_LIST_HIDDEN_FRAMEWORK_LABELS = {
    "openclaw": "OpenClaw",
    "hermes": "Hermes",
}


@click.command(
    context_settings=CONTEXT_SETTINGS,
    hidden=True,
    cls=CompatibilityAliasCommand,
    canonical_command="agentengine agent status",
)
@click.argument("agent_ref", required=False)
@click.option("--agent", "--agent-id", "agent_option", "-a", help="Agent 名称或 ID")
@click.option("--all", "show_all", is_flag=True, help="显示所有 Agent")
@click.option("--watch", "-w", is_flag=True, help="Watch 模式，持续刷新")
@click.option("--interval", "-i", default=2, help="Watch 刷新间隔 (秒)")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--account-id", envvar="KSYUN_ACCOUNT_ID", help="金山云账号 ID")
@dry_run_option()
def status(
    agent_ref: str,
    agent_option: str,
    show_all: bool,
    watch: bool,
    interval: int,
    region: str,
    account_id: str,
    dry_run: bool,
):
    """查看 Agent 的运行状态和 Endpoint。"""
    run_status_command(
        agent_ref=agent_ref,
        agent_option=agent_option,
        show_all=show_all,
        watch=watch,
        interval=interval,
        region=region,
        account_id=account_id,
        dry_run=dry_run,
        compatibility_alias=True,
    )


def run_status_command(
    *,
    agent_ref: str | None,
    agent_option: str | None,
    show_all: bool,
    watch: bool,
    interval: int,
    region: str,
    account_id: str | None,
    dry_run: bool,
    framework: str | Sequence[str] | None = None,
    compatibility_alias: bool = False,
    page: int = 1,
    size: int = 20,
):
    """查看 Agent 的运行状态和 Endpoint。"""
    dry_run = effective_dry_run(dry_run)
    if compatibility_alias:
        legacy = "agentengine status --all" if show_all else "agentengine status"
        canonical = "agentengine agent list" if show_all else "agentengine agent status"
        print_compatibility_hint(legacy=legacy, canonical=canonical)

    try:
        agent_input = merge_agent_inputs(
            agent_option=agent_option,
            positional_agent=agent_ref,
        )
    except ValueError as e:
        raise usage_error(str(e))

    agent = None
    if show_all and agent_input:
        raise usage_error("--all 与 Agent 参数不能同时使用")

    if not show_all:
        resolved = resolve_agent_ref(
            agent_input,
            cwd=Path("."),
            include_state=True,
            include_project_config=True,
        )
        if not resolved:
            raise resolution_error(
                "请指定 Agent（--agent 或位置参数），或在当前目录提供可解析的本地配置",
                hints=["自动解析顺序: .agentengine.state -> agentengine.yaml/ksadk.yaml", "请先执行 `agentengine agent list`。"],
            )
        agent = resolved.value
        if resolved.source != "cli":
            print_info(f"未显式指定 Agent，使用 {resolved.source_text}: {agent}")

    # 检查账号 ID
    if not account_id:
        raise validation_error(
            "需要金山云账号 ID",
            hints=["设置 KSYUN_ACCOUNT_ID 环境变量或使用 --account-id 参数。"],
        )

    if watch and dry_run:
        raise usage_error("Watch 模式不支持 dry-run，请去掉 --watch 或取消 dry-run。")
    if watch and is_json_output():
        raise usage_error("Watch 模式不支持 `--output json`。", hints=["请去掉 `--watch` 或改用 `agentengine agent status --output json`。"])

    # Dry Run 模式由 AgentEngineClient 处理
    # 只要传入 dry_run=True，底层 client 会抛出 DryRunExit 异常

    if show_all:
        run_async_with_dry_run(
            _list_all_agents(region, account_id, dry_run, page=page, size=size, framework=framework),
            dry_run=dry_run,
            dry_run_resource="agent",
            dry_run_action="list",
        )
    elif watch:
        _watch_status(agent, region, account_id, interval)
    else:
        run_async_with_dry_run(
            _show_agent_status(agent, region, account_id, dry_run),
            dry_run=dry_run,
            dry_run_resource="agent",
            dry_run_action="status",
        )


def _watch_status(agent: str, region: str, account_id: str, interval: int):
    """Watch 模式 - 持续刷新状态"""
    click.echo(f"Watch 模式启动 (每 {interval} 秒刷新)")
    click.echo("按 Ctrl+C 退出\n")

    try:
        while True:
            # 清屏
            click.clear()

            # 显示标题
            click.secho(f"Agent Status Monitor", fg="blue", bold=True)
            click.echo(f"   Agent: {agent}")
            click.echo(f"   Region: {region}")
            click.echo(f"   更新时间: {datetime.now().strftime('%H:%M:%S')}")
            click.echo("-" * 50)

            # 获取并显示状态
            try:
                asyncio.run(_show_agent_status_compact(agent, region, account_id))
            except Exception as e:
                print_exception("获取状态失败", e)

            click.echo("")
            click.echo(f"下次刷新: {interval} 秒后 (Ctrl+C 退出)")

            time.sleep(interval)

    except KeyboardInterrupt:
        click.echo("\n\n退出 Watch 模式")


async def _show_agent_status(
    agent: str,
    region: str,
    account_id: str,
    dry_run: bool = False,
):
    """显示单个 Agent 的状态 (完整)"""
    if not is_json_output():
        click.echo(f"查询 Agent 状态... (region: {region})")

    try:
        # 先按 ID 查询，失败后再按名称查询，避免依赖字符串前缀判断。
        result = await _get_agent_runtime(agent, region, account_id, dry_run, is_name=False)
        if result.get("status") == "Error":
            result_by_name = await _get_agent_runtime(agent, region, account_id, dry_run, is_name=True)
            if result_by_name.get("status") != "Error":
                result = result_by_name

    except DryRunExit:
        raise

    status_value = result.get("status", "Unknown")
    replicas = result.get("replicas", 0)
    ready = result.get("readyReplicas", 0)
    endpoint = result.get("endpoint")
    langfuse_url = result.get("langfuseTraceUrl")
    from dateutil import parser

    created_at = result.get('createdAt')
    if created_at:
        dt = parser.parse(created_at)
        created_at = dt.astimezone().isoformat()
    else:
        created_at = "-"

    updated_at = result.get('updatedAt')
    if updated_at:
        dt = parser.parse(updated_at)
        updated_at = dt.astimezone().isoformat()
    else:
        updated_at = "-"

    message = result.get("message")
    error = result.get("error")
    fields = [
        ("名称", str(result.get("agentRuntimeName", agent)), None),
        ("ID", str(result.get("agentRuntimeId", "N/A")), None),
        ("描述", str(result.get("description", "-")), None),
        ("状态", str(status_value), status_rich_style(status_value)),
        ("阶段", str(result.get("phase", "-")), None),
        ("副本", f"{ready}/{replicas}", replica_rich_style(ready, replicas)),
        ("Endpoint", str(endpoint or "未就绪"), "#58a6ff" if endpoint else "warn"),
        ("Langfuse", str(langfuse_url or "-"), "#58a6ff" if langfuse_url else None),
        ("创建时间", created_at, None),
        ("更新时间", updated_at, None),
    ]
    if message:
        fields.append(("消息", str(message), None))
    if error:
        fields.append(("错误", str(error), "err"))
    render_resource_status(
        title="Agent 状态",
        subtitle=f"region: {region}",
        fields=fields,
        resource="agent",
        item={
            "id": str(result.get("agentRuntimeId", "N/A")),
            "name": str(result.get("agentRuntimeName", agent)),
            "description": str(result.get("description", "-")),
            "status": str(status_value),
            "phase": str(result.get("phase", "-")),
            "replicas": {"ready": int(ready), "total": int(replicas)},
            "endpoint": str(endpoint or ""),
            "langfuse_url": str(langfuse_url or ""),
            "created_at": created_at,
            "updated_at": updated_at,
            "message": str(message or ""),
            "error": str(error or ""),
            "region": region,
        },
    )


async def _show_agent_status_compact(agent: str, region: str, account_id: str):
    """显示单个 Agent 的状态 (紧凑，用于 watch)"""
    # Watch 模式不支持 dry_run，默认为 False
    result = await _get_agent_runtime(agent, region, account_id, False, is_name=False)
    if result.get("status") == "Error":
        result_by_name = await _get_agent_runtime(agent, region, account_id, False, is_name=True)
        if result_by_name.get("status") != "Error":
            result = result_by_name

    # 状态
    status_value = result.get("status", "Unknown")
    status_color = _get_status_color(status_value)
    phase = result.get("phase", "-")

    click.echo(f"  状态:     {click.style(status_value, fg=status_color)} ({phase})")

    # 副本
    replicas = result.get("replicas", 0)
    ready = result.get("readyReplicas", 0)
    replica_color = "green" if ready == replicas and replicas > 0 else "yellow"
    click.echo(f"  副本:     {click.style(f'{ready}/{replicas}', fg=replica_color)}")

    # Endpoint
    endpoint = result.get("endpoint")
    if endpoint:
        click.echo(f"  Endpoint: {click.style(endpoint, fg='cyan')}")
    else:
        click.echo(f"  Endpoint: {click.style('待分配...', fg='yellow')}")

    # 消息
    message = result.get("message")
    if message:
        click.echo(f"  消息:     {message}")


async def _list_all_agents(
    region: str,
    account_id: str,
    dry_run: bool = False,
    *,
    page: int = 1,
    size: int = 20,
    framework: str | Sequence[str] | None = None,
):
    """列出 Agent 列表；未显式筛选专用框架时默认隐藏它们。"""
    if not is_json_output():
        click.echo(f"查询 Agent 列表... (region: {region}, page: {page}, size: {size})")

    try:
        normalized_frameworks = _normalize_framework_filters(framework)
        hidden_frameworks = {
            item for item in AGENT_LIST_HIDDEN_FRAMEWORKS if item not in normalized_frameworks
        }
        server_page = 1
        server_page_size = max(size, 100)
        all_visible_results = []
        hidden_special_agents = 0
        while True:
            response = await _list_agent_runtimes(
                region,
                account_id,
                dry_run,
                page=server_page,
                page_size=server_page_size,
                framework=",".join(normalized_frameworks) if normalized_frameworks else None,
            )
            raw_results = response.get("agents", [])
            if not raw_results:
                break

            if hidden_frameworks:
                filtered_results = [
                    r
                    for r in raw_results
                    if str(r.get("framework", "")).strip().lower() not in hidden_frameworks
                ]
                hidden_special_agents += len(raw_results) - len(filtered_results)
            else:
                filtered_results = raw_results
            all_visible_results.extend(filtered_results)

            if len(raw_results) < server_page_size:
                break
            server_page += 1
    except DryRunExit:
        raise

    hidden_framework_label = _format_hidden_framework_labels(hidden_frameworks)
    if hidden_special_agents > 0:
        print_info(
            f"已隐藏 {hidden_special_agents} 个 {hidden_framework_label} 实例"
            "（使用对应的专用 list 命令查看）"
        )

    start = max(page - 1, 0) * size
    end = start + size
    results = all_visible_results[start:end]
    visible_total = len(all_visible_results)

    unhealthy = 0
    running = 0
    rows = []
    items = []
    for r in results:
        agent_id = (r.get("agentRuntimeId") or "N/A")
        name = (r.get("agentRuntimeName") or "N/A")
        status_val = r.get("status", "Unknown")
        ready = int(r.get("readyReplicas", 0) or 0)
        replicas = int(r.get("replicas", 0) or 0)
        endpoint = r.get("endpoint", "-") or "-"

        status_upper = status_val.upper()
        status_style = status_rich_style(status_upper)
        replica_style = replica_rich_style(ready, replicas)
        if status_upper in {"RUNNING", "READY", "HEALTHY"}:
            running += 1
        else:
            unhealthy += 1

        rows.append(
            (
                agent_id,
                name,
                f"[{status_style}]{status_upper}[/]",
                f"[{replica_style}]{ready}/{replicas}[/]",
                endpoint,
            )
        )
        items.append(
            {
                "id": agent_id,
                "name": name,
                "status": status_upper,
                "replicas": {"ready": ready, "total": replicas},
                "endpoint": endpoint,
                "framework": str(r.get("framework") or ""),
            }
        )

    render_resource_list(
        title=f"Agent 列表  [muted](region: {region})[/]",
        noun="Agent",
        columns=(
            {"header": "ID", "key": "id", "style": "#58a6ff", "no_wrap": True, "min_width": 24},
            {"header": "名称", "key": "name", "style": "#c9d1d9", "min_width": 20},
            {"header": "状态", "key": "status", "no_wrap": True, "justify": "center", "min_width": 10},
            {"header": "副本", "key": "replicas_display", "no_wrap": True, "justify": "center", "min_width": 8},
            {"header": "Endpoint", "key": "endpoint", "style": "#8b949e", "overflow": "ellipsis"},
        ),
        rows=rows,
        total=visible_total,
        page=page,
        size=size,
        empty_message="没有找到已部署的 Agent",
        summary_lines=[
            f"健康: {running}  待关注: {unhealthy}",
            *(
                [f"已隐藏 {hidden_special_agents} 个 {hidden_framework_label} 实例"]
                if hidden_special_agents > 0
                else []
            ),
        ],
        resource="agent",
        items=[
            {
                **item,
                "replicas_display": f"{item['replicas']['ready']}/{item['replicas']['total']}",
            }
            for item in items
        ],
    )
    if not is_json_output():
        console.print(summary_panel(total=len(results), healthy=running, attention=unhealthy, noun="Agent"))


def _get_status_color(status: str) -> str:
    """根据状态返回颜色"""
    return status_click_color(status)


async def _get_agent_runtime(agent: str, region: str, account_id: str, dry_run: bool = False, is_name: bool = False) -> dict:
    """获取 Agent 运行时状态

    调用 AgentEngine Server API
    """
    from ksadk.api import AgentEngineClient
    try:
        extra_headers = {}
        # 传递 Account ID
        if account_id:
            extra_headers["X-Ksc-Account-Id"] = account_id

        async with AgentEngineClient(region=region, dry_run=dry_run, extra_headers=extra_headers) as client:
            if is_name:
                response = await client.get_agent(name=agent)
            else:
                response = await client.get_agent(agent_id=agent)

            # _action 已统一转为 snake_case
            basic = response.get("basic", {})
            deploy = response.get("deployment", {})
            quick = response.get("quick_access", {})
            adv = response.get("advanced", {})

            return {
                "agentRuntimeId": basic.get("agent_id", "") or response.get("agent_id", ""),
                "agentRuntimeName": basic.get("name", "") or response.get("name", ""),
                "description": basic.get("description", "") or response.get("description", ""),
                "status": basic.get("status", "") or response.get("status", "Unknown"),
                "phase": basic.get("phase", "") or response.get("phase", ""),
                "replicas": basic.get("replicas") if basic.get("replicas") is not None else response.get("replicas", deploy.get("scaling", {}).get("min_replicas", 1)),
                "readyReplicas": basic.get("ready_replicas") if basic.get("ready_replicas") is not None else response.get("ready_replicas", 0),
                "endpoint": quick.get("public_endpoint") or quick.get("private_endpoint") or response.get("endpoint", ""),
                "langfuseTraceUrl": adv.get("observability_url", "") or response.get("langfuse_trace_url", ""),
                "createdAt": basic.get("created_at", "") or response.get("created_at", ""),
                "updatedAt": basic.get("updated_at", "") or response.get("updated_at", ""),
                "message": basic.get("message", "") or response.get("message", ""),
            }
    except DryRunExit:
        raise
    except Exception as e:
        return {
            "agentRuntimeName": agent,
            "status": "Error",
            "message": f"查询失败: {str(e)}",
            "error": str(e),
        }


async def _list_agent_runtimes(
    region: str,
    account_id: str,
    dry_run: bool = False,
    *,
    page: int = 1,
    page_size: int = 20,
    framework: str | Sequence[str] | None = None,
) -> dict:
    """列出 Agent 运行时

    调用 AgentEngine Server API
    """
    from ksadk.api import AgentEngineClient
    try:
        extra_headers = {}
        # 传递 Account ID
        if account_id:
            extra_headers["X-Ksc-Account-Id"] = account_id

        async with AgentEngineClient(region=region, dry_run=dry_run, extra_headers=extra_headers) as client:
            response = await client.list_agents(
                region=region,
                framework=framework,
                page=page,
                page_size=page_size,
            )
            agents = response.get("agents", []) or []
            results = []
            for agent in agents:
                results.append(
                    {
                        "agentRuntimeId": agent.get("agent_id", ""),
                        "agentRuntimeName": agent.get("name", ""),
                        "status": agent.get("status", ""),
                        "replicas": agent.get("replicas", 0),
                        "readyReplicas": agent.get("ready_replicas", 0),
                        "endpoint": agent.get("endpoint", ""),
                        "framework": agent.get("framework", ""),
                    }
                )

            return {
                "agents": results,
                "total": response.get("total", len(results)),
            }
    except DryRunExit:
        raise
    except Exception as e:
        if not is_json_output():
            click.secho(f"查询失败: {str(e)}", fg="red")
        return {"agents": [], "total": 0}


def _normalize_framework_filters(framework: str | Sequence[str] | None) -> list[str]:
    """规范化 CLI framework 过滤，支持 CSV 和字符串序列。"""
    if framework is None:
        return []

    raw_values: list[str]
    if isinstance(framework, str):
        raw_values = [framework]
    else:
        raw_values = [str(item) for item in framework if str(item).strip()]

    normalized_values: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        for part in str(raw).split(","):
            normalized = part.strip().lower()
            if normalized and normalized not in seen:
                seen.add(normalized)
                normalized_values.append(normalized)
    return normalized_values


def _format_hidden_framework_labels(frameworks: set[str]) -> str:
    labels = [
        AGENT_LIST_HIDDEN_FRAMEWORK_LABELS.get(framework, framework)
        for framework in AGENT_LIST_HIDDEN_FRAMEWORKS
        if framework in frameworks
    ]
    return "/".join(labels) if labels else "专用框架"
