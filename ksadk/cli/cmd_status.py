"""
agentengine status - 查看 Agent 运行状态和 Endpoint

支持 watch 模式实时刷新
"""

import click
import asyncio
import time
import os
import json
import uuid
from pathlib import Path
from typing import Optional
from datetime import datetime
from datetime import datetime
from ksadk.api.client import DryRunExit


@click.command()
@click.option("--agent", "-a", help="Agent 名称或 ID")
@click.option("--all", "show_all", is_flag=True, help="显示所有 Agent")
@click.option("--watch", "-w", is_flag=True, help="Watch 模式，持续刷新")
@click.option("--interval", "-i", default=2, help="Watch 刷新间隔 (秒)")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--account-id", envvar="KSYUN_ACCOUNT_ID", help="金山云账号 ID")
@click.option("--dry-run", is_flag=True, help="只打印 curl 请求，不执行")
def status(
    agent: str,
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
        agentengine status --agent my-agent
        agentengine status --agent my-agent --watch
        agentengine status --all
        agentengine status --agent my-agent --dry-run
    """
    if not agent and not show_all:
        # 尝试从当前目录的配置文件读取 agent 名称
        agent = _get_agent_from_config()

        if not agent:
            click.secho("错误: 请指定 --agent 或使用 --all 查看所有", fg="red")
            click.echo("提示: 也可以在包含 agentengine.yaml 的目录下运行")
            raise SystemExit(1)

    # 检查账号 ID
    if not account_id:
        click.secho("错误: 需要金山云账号 ID", fg="red")
        click.echo("提示: 设置 KSYUN_ACCOUNT_ID 环境变量或使用 --account-id 参数")
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


def _get_agent_from_config() -> Optional[str]:
    """从配置文件读取 agent 名称"""
    import yaml

    config_path = Path(".") / "agentengine.yaml"
    if not config_path.exists():
        config_path = Path(".") / "ksadk.yaml"

    if config_path.exists():
        # 使用 utf-8-sig 自动处理 BOM，确保 Windows 兼容性
        with open(config_path, encoding='utf-8-sig') as f:
            config = yaml.safe_load(f)
            return config.get("name")
    return None


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
                click.secho(f"获取状态失败: {e}", fg="red")

            click.echo("")
            click.echo(f"下次刷新: {interval} 秒后 (Ctrl+C 退出)")

            time.sleep(interval)

    except KeyboardInterrupt:
        click.echo("\n\n退出 Watch 模式")


async def _show_agent_status(agent: str, region: str, account_id: str, dry_run: bool = False):
    """显示单个 Agent 的状态 (完整)"""
    click.echo(f"查询 Agent 状态... (region: {region})")

    try:
        result = await _get_agent_runtime(agent, region, account_id, dry_run)
    except DryRunExit:
        raise

    click.echo("")
    click.secho("Agent 状态", fg="blue", bold=True)
    click.echo("-" * 50)

    # 基本信息
    click.echo(f"  名称:       {result.get('agentRuntimeName', agent)}")
    click.echo(f"  ID:         {result.get('agentRuntimeId', 'N/A')}")
    click.echo(f"  描述:       {result.get('description', '-')}")

    # 状态
    status_value = result.get("status", "Unknown")
    status_color = _get_status_color(status_value)
    click.echo(f"  状态:       {click.style(status_value, fg=status_color)}")
    click.echo(f"  阶段:       {result.get('phase', '-')}")

    # 副本
    replicas = result.get("replicas", 0)
    ready = result.get("readyReplicas", 0)
    click.echo(f"  副本:       {ready}/{replicas}")

    # Endpoint
    endpoint = result.get("endpoint")
    if endpoint:
        click.echo(f"  Endpoint:   {click.style(endpoint, fg='cyan')}")
    else:
        click.echo(f"  Endpoint:   {click.style('未就绪', fg='yellow')}")

    # Langfuse
    langfuse_url = result.get("langfuseTraceUrl")
    if langfuse_url:
        click.echo(f"  Langfuse:   {langfuse_url}")

    # 时间
    from dateutil import parser
    click.echo("")
    
    created_at = result.get('createdAt')
    if created_at:
        dt = parser.parse(created_at)
        click.echo(f"  创建时间:   {dt.astimezone().isoformat()}")
    else:
        click.echo(f"  创建时间:   -")
        
    updated_at = result.get('updatedAt')
    if updated_at:
       dt = parser.parse(updated_at)
       click.echo(f"  更新时间:   {dt.astimezone().isoformat()}")
    else:
       click.echo(f"  更新时间:   -")

    # 消息
    message = result.get("message")
    if message:
        click.echo("")
        click.echo(f"  消息:       {message}")

    # 错误信息
    error = result.get("error")
    if error:
        click.echo("")
        click.secho(f"  错误:       {error}", fg="red")

    click.echo("")


async def _show_agent_status_compact(agent: str, region: str, account_id: str):
    """显示单个 Agent 的状态 (紧凑，用于 watch)"""
    # Watch 模式不支持 dry_run，默认为 False
    result = await _get_agent_runtime(agent, region, account_id, False)

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
    click.secho("Agent 列表", fg="blue", bold=True)
    click.echo("-" * 130)
    click.echo(f"{'ID':<22} {'名称':<30} {'状态':<12} {'副本':<8} {'Endpoint':<50}")
    click.echo("-" * 130)

    if not results:
        click.secho("(暂无数据)", fg="yellow")
    else:
        for r in results:
            agent_id = r.get("agentRuntimeId", "N/A")[:48]
            name = r.get("agentRuntimeName", "N/A")[:48]
            status_val = r.get("status", "Unknown")
            status_color = _get_status_color(status_val)
            replicas = f"{r.get('readyReplicas', 0)}/{r.get('replicas', 0)}"
            endpoint = r.get("endpoint", "-")[:128] if r.get("endpoint") else "-"

            click.echo(
                f"{agent_id:<22} {name:<30} {click.style(status_val, fg=status_color):<22} {replicas:<8} {endpoint:<50}"
            )

    click.echo("")
    click.echo(f"共 {len(results)} 个 Agent")


def _get_status_color(status: str) -> str:
    """根据状态返回颜色"""
    status_colors = {
        "Running": "green",
        "Ready": "green",
        "Healthy": "green",
        "Creating": "yellow",
        "Pending": "yellow",
        "Updating": "yellow",
        "Scaling": "yellow",
        "Failed": "red",
        "Error": "red",
        "Terminated": "red",
        "Unknown": "white",
    }
    return status_colors.get(status, "white")


async def _get_agent_runtime(agent: str, region: str, account_id: str, dry_run: bool = False) -> dict:
    """获取 Agent 运行时状态

    调用 AgentEngine Server API
    """
    from ksadk.api import AgentEngineClient
    from ksadk.common.auth import AWSV4Auth

    try:
        # 获取本地凭证透传给 Server
        auth = AWSV4Auth()
        extra_headers = {} # Initialize extra_headers
        if auth.access_key_id and auth.secret_access_key:
            extra_headers["X-Ksyun-Access-Key"] = auth.access_key_id
            extra_headers["X-Ksyun-Secret-Key"] = auth.secret_access_key
        
        # 传递 Account ID
        if account_id:
            extra_headers["X-Ksyun-Account-Id"] = account_id

        async with AgentEngineClient(region=region, dry_run=dry_run, extra_headers=extra_headers) as client:
            response = await client.get_agent(agent)

            return {
                "agentRuntimeId": response.get("agent_id", ""),
                "agentRuntimeName": response.get("name", ""),
                "description": response.get("description", ""),
                "status": response.get("status", "Unknown"),
                "phase": response.get("phase", ""),
                "replicas": response.get("replicas", 0),
                "readyReplicas": response.get("ready_replicas", 0),
                "endpoint": response.get("endpoint", ""),
                "langfuseTraceUrl": response.get("langfuse_trace_url", ""),
                "createdAt": response.get("created_at", ""),
                "updatedAt": response.get("updated_at", ""),
                "message": response.get("message", ""),
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
    from ksadk.common.auth import AWSV4Auth

    try:
        # 获取本地凭证透传给 Server
        auth = AWSV4Auth()
        extra_headers = {} # Initialize extra_headers
        if auth.access_key_id and auth.secret_access_key:
            extra_headers["X-Ksyun-Access-Key"] = auth.access_key_id
            extra_headers["X-Ksyun-Secret-Key"] = auth.secret_access_key
        
        # 传递 Account ID
        if account_id:
            extra_headers["X-Ksyun-Account-Id"] = account_id

        async with AgentEngineClient(region=region, dry_run=dry_run, extra_headers=extra_headers) as client:
            response = await client.list_agents(region=region)

            results = []
            for agent in response.get("Agents", []):
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



