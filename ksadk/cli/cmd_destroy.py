"""
agentengine destroy - 停止并销毁 Agent 实例
"""

import click
import asyncio
import os
import json
import uuid
from pathlib import Path
from ksadk.api.client import DryRunExit
from ksadk.deployment import DeploymentManager, DeployTarget


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

            # 使用 utf-8-sig 自动处理 BOM，确保 Windows 兼容性
            with open(config_path, encoding='utf-8-sig') as f:
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

    # Dry Run 提示
    if dry_run:
        click.secho(f"[Dry Run] 准备销毁 Agent: {agent} (Region: {region})", fg="yellow")
    else:
        click.secho(f"即将销毁 Agent: {agent}", fg="yellow", bold=True)
        click.echo(f"   区域: {region}")

    if not force and not dry_run:
        if not click.confirm(f"确定要销毁 Agent '{agent}' 吗? 此操作不可恢复"):
            click.echo("已取消")
            return

    # 构造 Provider 和 Target
    provider_name = "serverless"  # 目前默认 serverless
    
    try:
        provider = DeploymentManager.get_provider(provider_name)
    except ValueError as e:
        click.secho(f"错误: {e}", fg="red")
        raise SystemExit(1)

    deploy_target = DeployTarget(
        provider=provider_name,
        region=region,
        extra={
            "account_id": account_id,
            "dry_run": dry_run
        }
    )

    if not dry_run:
        click.echo("")
        click.echo("正在停止 Agent 实例...")

    # 调用 Provider 销毁
    try:
        success = asyncio.run(provider.destroy(agent, deploy_target))

        if success:
            click.secho("\nAgent 已销毁!", fg="green")
        else:
            if not dry_run:
                click.secho("\n销毁失败，请检查错误信息", fg="red")
                raise SystemExit(1)
            
    except DryRunExit:
        pass  # Dry Run 完成
    except Exception as e:
        click.secho(f"操作失败: {e}", fg="red")
        raise SystemExit(1)

