"""
agentengine destroy - 停止并销毁 Agent 实例
"""

import click
import asyncio
import os
import json
import uuid
from pathlib import Path
from ksadk.common.constants import DEFAULT_SERVERLESS_ENDPOINT


@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.option("--agent", "-a", help="Agent 名称或 ID")
@click.option("--force", "-f", is_flag=True, help="强制删除，不提示确认")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--account-id", envvar="KSYUN_ACCOUNT_ID", help="金山云账号 ID")
@click.option("--dry-run", is_flag=True, help="只打印 curl 请求，不执行")
def destroy(agent: str, force: bool, region: str, account_id: str, dry_run: bool):
    """停止并销毁 Agent 实例，释放相关资源

    \b
    示例:
        agentengine destroy --agent my-agent
        agentengine destroy --agent my-agent --force
        agentengine destroy --agent my-agent --dry-run
    """
    if not agent:
        # 尝试从配置文件读取
        config_path = Path(".") / "agentengine.yaml"
        if not config_path.exists():
            config_path = Path(".") / "ksadk.yaml"

        if config_path.exists():
            import yaml

            with open(config_path) as f:
                config = yaml.safe_load(f)
                agent = config.get("name")

        if not agent:
            click.secho("错误: 请指定 --agent 参数", fg="red")
            raise SystemExit(1)

    # 检查账号 ID
    if not account_id:
        click.secho("错误: 需要金山云账号 ID", fg="red")
        click.echo("提示: 设置 KSYUN_ACCOUNT_ID 环境变量或使用 --account-id 参数")
        raise SystemExit(1)

    # Dry Run 模式
    if dry_run:
        _print_destroy_curl(agent, region, account_id)
        return

    click.secho(f"即将销毁 Agent: {agent}", fg="yellow", bold=True)
    click.echo(f"   区域: {region}")

    if not force:
        if not click.confirm(f"确定要销毁 Agent '{agent}' 吗? 此操作不可恢复"):
            click.echo("已取消")
            return

    click.echo("")
    click.echo("正在停止 Agent 实例...")

    # 调用 Serverless API 删除
    success = asyncio.run(_delete_agent_runtime(agent, region, account_id))

    if success:
        click.secho("\nAgent 已销毁!", fg="green")
    else:
        click.secho("\n销毁失败，请检查错误信息", fg="red")
        raise SystemExit(1)


async def _delete_agent_runtime(agent: str, region: str, account_id: str) -> bool:
    """删除 Agent 运行时

    调用 Serverless API - DeleteAgentRuntime
    """
    from ksadk.deployment.providers.serverless_api import (
        ServerlessAPIClient,
        DeleteAgentRuntimeInput,
        GetAgentRuntimeInput,
        ServerlessAPIError,
    )

    client = ServerlessAPIClient(
        account_id=account_id,
        region=region,
    )

    try:
        # 如果传入的是名称，先查询获取 ID
        agent_id = agent
        if not agent.startswith("ar-"):
            click.echo(f"正在查询 Agent ID...")
            get_input = GetAgentRuntimeInput(agent_runtime_name=agent)
            try:
                get_response = await client.get_agent_runtime(get_input)
                agent_id = get_response.agent_runtime_id
                click.echo(f"  Agent ID: {agent_id}")
            except ServerlessAPIError as e:
                click.secho(f"  查询失败: {e.message}", fg="red")
                return False

        # 执行删除
        click.echo(f"正在删除 Agent...")
        delete_input = DeleteAgentRuntimeInput(agent_runtime_id=agent_id)
        response = await client.delete_agent_runtime(delete_input)

        click.echo(f"  状态: {response.status}")
        click.echo(f"  请求 ID: {response.request_id}")

        return True

    except ServerlessAPIError as e:
        click.secho(f"删除失败: {e.message}", fg="red")
        if e.request_id:
            click.echo(f"  请求 ID: {e.request_id}")
        return False
    finally:
        await client.close()


def _print_destroy_curl(agent: str, region: str, account_id: str):
    """打印 destroy 的 curl 请求"""
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
    click.echo(f"   Agent:      {agent}")

    # 如果是名称，需要先查询 ID
    if not agent.startswith("ar-"):
        click.echo(f"\n注意: 传入的是 Agent 名称，实际删除时需要先查询 ID")
        click.echo("")

        # Step 1: GetAgentRuntime
        click.secho("Step 1: 查询 Agent ID (GetAgentRuntime)", fg="cyan")
        get_body = {"agentRuntimeName": agent}
        click.echo(f"\n请求体:")
        click.echo(json.dumps(get_body, indent=2, ensure_ascii=False))

        get_body_json = json.dumps(get_body, ensure_ascii=False)
        click.echo(f"\ncurl 命令:")
        click.echo("-" * 60)
        curl_get = f'''curl -X POST "{endpoint}/GetAgentRuntime" \\
  -H "Content-Type: application/json" \\
  -H "Accept: application/json" \\
  -H "X-Ksc-Request-Id: {request_id}" \\
  -H "X-Ksc-Account-Id: {account_id}" \\
  -H "X-Ksc-Region: {region}" \\
  -d '{get_body_json}' '''
        click.echo(curl_get)
        click.echo("-" * 60)

        # Step 2: DeleteAgentRuntime
        click.echo("")
        click.secho("Step 2: 删除 Agent (DeleteAgentRuntime)", fg="cyan")
        delete_body = {"agentRuntimeId": "<从 Step 1 响应中获取>"}
    else:
        delete_body = {"agentRuntimeId": agent}

    click.echo(f"\n请求体:")
    click.echo(json.dumps(delete_body, indent=2, ensure_ascii=False))

    delete_body_json = json.dumps(delete_body, ensure_ascii=False)
    request_id2 = str(uuid.uuid4())

    click.echo(f"\ncurl 命令 (需要 AWS V4 签名):")
    click.echo("-" * 60)
    curl_delete = f'''curl -X POST "{endpoint}/DeleteAgentRuntime" \\
  -H "Content-Type: application/json" \\
  -H "Accept: application/json" \\
  -H "X-Ksc-Request-Id: {request_id2}" \\
  -H "X-Ksc-Account-Id: {account_id}" \\
  -H "X-Ksc-Region: {region}" \\
  -d '{delete_body_json}' '''
    click.echo(curl_delete)
    click.echo("-" * 60)

    click.secho("\n注意: 实际请求需要 AWS V4 签名，上述 curl 仅供参考", fg="yellow")
    click.echo("环境变量:")
    click.echo(f"   KSYUN_ACCOUNT_ID = {account_id}")
    click.echo(f"   KSYUN_ACCESS_KEY = {ak[:8] + '****' if ak else '(未设置)'}")
    click.echo(f"   KSYUN_SECRET_KEY = {'****' if sk else '(未设置)'}")
