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
from ksadk.common.constants import DEFAULT_SERVERLESS_ENDPOINT


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

    # Dry Run 模式
    if dry_run:
        _print_status_curl(agent, show_all, region, account_id)
        return

    if show_all:
        asyncio.run(_list_all_agents(region, account_id))
    elif watch:
        _watch_status(agent, region, account_id, interval)
    else:
        asyncio.run(_show_agent_status(agent, region, account_id))


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


async def _show_agent_status(agent: str, region: str, account_id: str):
    """显示单个 Agent 的状态 (完整)"""
    click.echo(f"查询 Agent 状态... (region: {region})")

    result = await _get_agent_runtime(agent, region, account_id)

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
    click.echo("")
    click.echo(f"  创建时间:   {result.get('createdAt', '-')}")
    click.echo(f"  更新时间:   {result.get('updatedAt', '-')}")

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
    result = await _get_agent_runtime(agent, region, account_id)

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


async def _list_all_agents(region: str, account_id: str):
    """列出所有 Agent"""
    click.echo(f"查询 Agent 列表... (region: {region})")

    results = await _list_agent_runtimes(region, account_id)

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


async def _get_agent_runtime(agent: str, region: str, account_id: str) -> dict:
    """获取 Agent 运行时状态

    调用 Serverless API - GetAgentRuntime
    """
    from ksadk.deployment.providers.serverless_api import (
        ServerlessAPIClient,
        GetAgentRuntimeInput,
        ServerlessAPIError,
    )

    client = ServerlessAPIClient(
        account_id=account_id,
        region=region,
    )

    try:
        # 支持通过 ID 或名称查询
        input_obj = GetAgentRuntimeInput(
            agent_runtime_id=agent if agent.startswith("ar-") else "",
            agent_runtime_name=agent if not agent.startswith("ar-") else "",
        )

        response = await client.get_agent_runtime(input_obj)

        return {
            "agentRuntimeId": response.agent_runtime_id,
            "agentRuntimeName": response.agent_runtime_name,
            "description": response.description,
            "status": response.status,
            "phase": response.phase,
            "replicas": response.replicas,
            "readyReplicas": response.ready_replicas,
            "endpoint": response.endpoint,
            "langfuseTraceUrl": response.langfuse_trace_url,
            "createdAt": response.created_at,
            "updatedAt": response.updated_at,
            "message": response.message,
        }
    except ServerlessAPIError as e:
        return {
            "agentRuntimeName": agent,
            "status": "Error",
            "message": f"查询失败: {e.message}",
            "error": str(e),
        }
    finally:
        await client.close()


async def _list_agent_runtimes(region: str, account_id: str) -> list:
    """列出 Agent 运行时

    调用 Serverless API - ListAgentRuntimes
    """
    from ksadk.deployment.providers.serverless_api import (
        ServerlessAPIClient,
        ListAgentRuntimesInput,
        ServerlessAPIError,
    )

    client = ServerlessAPIClient(
        account_id=account_id,
        region=region,
    )

    try:
        input_obj = ListAgentRuntimesInput(
            page_num=0,
            page_size=1000,
        )

        response = await client.list_agent_runtimes(input_obj)

        results = []
        for runtime in response.agent_runtimes:
            results.append(
                {
                    "agentRuntimeId": runtime.agent_runtime_id,
                    "agentRuntimeName": runtime.agent_runtime_name,
                    "status": runtime.status,
                    "replicas": runtime.replicas,
                    "readyReplicas": runtime.ready_replicas,
                    "endpoint": runtime.endpoint,
                }
            )

        return results
    except ServerlessAPIError as e:
        click.secho(f"查询失败: {e.message}", fg="red")
        return []
    finally:
        await client.close()


def _print_status_curl(agent: str, show_all: bool, region: str, account_id: str):
    """打印 status/list 的 curl 请求"""
    endpoint = os.environ.get(
        "SERVERLESS_ENDPOINT",
        DEFAULT_SERVERLESS_ENDPOINT,
    )
    request_id = str(uuid.uuid4())

    ak = os.environ.get("KSYUN_ACCESS_KEY", "")
    sk = os.environ.get("KSYUN_SECRET_KEY", "")

    click.echo("=" * 60)
    click.secho("Dry Run 模式 - 只打印 curl 请求，不执行", fg="yellow", bold=True)
    click.echo("=" * 60)

    click.echo(f"\n配置信息:")
    click.echo(f"   Endpoint:   {endpoint}")
    click.echo(f"   Account ID: {account_id}")
    click.echo(f"   Region:     {region}")

    if show_all:
        # ListAgentRuntimes
        api_path = "/ListAgentRuntimes"
        request_body = {
            "pageNum": 0,
            "pageSize": 1000,
        }
        click.echo(f"   API:        ListAgentRuntimes")
    else:
        # GetAgentRuntime
        api_path = "/GetAgentRuntime"
        if agent.startswith("ar-"):
            request_body = {"agentRuntimeId": agent}
        else:
            request_body = {"agentRuntimeName": agent}
        click.echo(f"   API:        GetAgentRuntime")
        click.echo(f"   Agent:      {agent}")

    click.echo(f"\n请求体 (Request Body):")
    click.echo(json.dumps(request_body, indent=2, ensure_ascii=False))

    body_json = json.dumps(request_body, ensure_ascii=False)

    click.echo(f"\ncurl 命令 (需要 AWS V4 签名):")
    click.echo("-" * 60)

    curl_cmd = f'''curl -X POST "{endpoint}{api_path}" \\
  -H "Content-Type: application/json" \\
  -H "Accept: application/json" \\
  -H "X-Ksc-Request-Id: {request_id}" \\
  -H "X-Ksc-Account-Id: {account_id}" \\
  -H "X-Ksc-Region: {region}" \\
  -d '{body_json}' '''

    click.echo(curl_cmd)

    click.echo("-" * 60)
    click.secho("\n注意: 实际请求需要 AWS V4 签名，上述 curl 仅供参考", fg="yellow")
    click.echo("环境变量:")
    click.echo(f"   KSYUN_ACCOUNT_ID = {account_id}")
    click.echo(f"   KSYUN_ACCESS_KEY = {ak[:8] + '****' if ak else '(未设置)'}")
    click.echo(f"   KSYUN_SECRET_KEY = {'****' if sk else '(未设置)'}")
