"""
agentengine status - 查看 Agent 运行状态和 Endpoint

支持 watch 模式实时刷新
"""

import click
import asyncio
import time
from pathlib import Path
from datetime import datetime
from ksadk.api.client import DryRunExit
from ksadk.cli.agent_ref import merge_agent_inputs, resolve_agent_ref
from ksadk.cli.ui import (
    get_console,
    new_table,
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


@click.command()
@click.argument("agent_ref", required=False)
@click.option("--agent", "--agent-id", "agent_option", "-a", help="Agent 名称或 ID")
@click.option("--all", "show_all", is_flag=True, help="显示所有 Agent")
@click.option("--watch", "-w", is_flag=True, help="Watch 模式，持续刷新")
@click.option("--interval", "-i", default=2, help="Watch 刷新间隔 (秒)")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--account-id", envvar="KSYUN_ACCOUNT_ID", help="金山云账号 ID")
@click.option("--dry-run", is_flag=True, help="只打印 curl 请求，不执行")
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
    """查看 Agent 的运行状态和 Endpoint

    \b
    示例:
        # 1) 目录内自动解析 agent
        agentengine status
        # 2) 显式指定 agent
        agentengine status --agent ar-xxxx --watch --account-id X-Ksc-Account-Id
        # 3) 显式指定区域
        KSYUN_REGION=cn-beijing-6 agentengine status --agent ar-xxxx --account-id X-Ksc-Account-Id
    """
    try:
        agent_input = merge_agent_inputs(
            agent_option=agent_option,
            positional_agent=agent_ref,
        )
    except ValueError as e:
        print_error(f"错误: {e}")
        raise SystemExit(1)

    agent = None
    if show_all and agent_input:
        print_error("错误: --all 与 Agent 参数不能同时使用")
        raise SystemExit(1)

    if not show_all:
        resolved = resolve_agent_ref(
            agent_input,
            cwd=Path("."),
            include_state=True,
            include_project_config=True,
        )
        if not resolved:
            print_error("错误: 请指定 Agent（--agent 或位置参数），或在当前目录提供可解析的本地配置")
            print_info("自动解析顺序: .agentengine.state -> agentengine.yaml/ksadk.yaml")
            raise SystemExit(1)
        agent = resolved.value
        if resolved.source != "cli":
            print_info(f"未显式指定 Agent，使用 {resolved.source_text}: {agent}")

    # 检查账号 ID
    if not account_id:
        print_error("错误: 需要金山云账号 ID")
        print_info("提示: 设置 KSYUN_ACCOUNT_ID 环境变量或使用 --account-id 参数")
        raise SystemExit(1)

    # Dry Run 模式由 AgentEngineClient 处理
    # 只要传入 dry_run=True，底层 client 会抛出 DryRunExit 异常

    if show_all:
        try:
            asyncio.run(_list_all_agents(region, account_id, dry_run))
        except DryRunExit as e:
            pass
    elif watch:
        _watch_status(agent, region, account_id, interval)
    else:
        try:
            asyncio.run(_show_agent_status(agent, region, account_id, dry_run))
        except DryRunExit as e:
            pass


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
                print_error(f"获取状态失败: {e}")

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

    click.echo("")
    print_title("Agent 状态", f"region: {region}")

    # 基本信息
    print_kv("名称", result.get("agentRuntimeName", agent))
    print_kv("ID", result.get("agentRuntimeId", "N/A"))
    print_kv("描述", result.get("description", "-"))

    # 状态
    status_value = result.get("status", "Unknown")
    status_color = _get_status_color(status_value)
    print_kv("状态", status_value, value_style=status_rich_style(status_value))
    print_kv("阶段", result.get("phase", "-"))

    # 副本
    replicas = result.get("replicas", 0)
    ready = result.get("readyReplicas", 0)
    print_kv("副本", f"{ready}/{replicas}", value_style=replica_rich_style(ready, replicas))

    # Endpoint
    endpoint = result.get("endpoint")
    if endpoint:
        print_kv("Endpoint", endpoint, value_style="#58a6ff")
    else:
        print_warn("Endpoint: 未就绪")

    # Langfuse
    langfuse_url = result.get("langfuseTraceUrl")
    if langfuse_url:
        print_kv("Langfuse", langfuse_url, value_style="#58a6ff")

    # 时间
    from dateutil import parser
    print_info("")
    
    created_at = result.get('createdAt')
    if created_at:
        dt = parser.parse(created_at)
        print_kv("创建时间", dt.astimezone().isoformat())
    else:
        print_kv("创建时间", "-")
        
    updated_at = result.get('updatedAt')
    if updated_at:
       dt = parser.parse(updated_at)
       print_kv("更新时间", dt.astimezone().isoformat())
    else:
       print_kv("更新时间", "-")

    # 消息
    message = result.get("message")
    if message:
        print_kv("消息", message)

    # 错误信息
    error = result.get("error")
    if error:
        print_error(f"错误: {error}")


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


async def _list_all_agents(region: str, account_id: str, dry_run: bool = False):
    """列出所有 Agent"""
    click.echo(f"查询 Agent 列表... (region: {region})")

    try:
        results = await _list_agent_runtimes(region, account_id, dry_run)
    except DryRunExit:
        raise

    click.echo("")
    table = new_table(f"Agent 列表  [muted](region: {region})[/]")
    table.add_column("ID", style="#58a6ff", no_wrap=True, min_width=24)
    table.add_column("名称", style="#c9d1d9", min_width=20)
    table.add_column("状态", no_wrap=True, justify="center", min_width=10)
    table.add_column("副本", no_wrap=True, justify="center", min_width=8)
    table.add_column("Endpoint", style="#8b949e", overflow="ellipsis")

    if not results:
        table.add_row("-", "-", "[yellow]暂无数据[/]", "-", "-")
        console.print(table)
        console.print(summary_panel(total=0, healthy=0, attention=0, noun="Agent"))
        return

    unhealthy = 0
    running = 0
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

        table.add_row(
            agent_id,
            name,
            f"[{status_style}]{status_upper}[/]",
            f"[{replica_style}]{ready}/{replicas}[/]",
            endpoint,
        )

    console.print(table)
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


async def _list_agent_runtimes(region: str, account_id: str, dry_run: bool = False) -> list:
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
            response = await client.list_agents(region=region)

            results = []
            for agent in response.get("agents", []):
                results.append(
                    {
                        "agentRuntimeId": agent.get("agent_id", ""),
                        "agentRuntimeName": agent.get("name", ""),
                        "status": agent.get("status", ""),
                        "replicas": agent.get("replicas", 0),
                        "readyReplicas": agent.get("ready_replicas", 0),
                        "endpoint": agent.get("endpoint", ""),
                    }
                )

            return results
    except DryRunExit:
        raise
    except Exception as e:
        click.secho(f"查询失败: {str(e)}", fg="red")
        return []
