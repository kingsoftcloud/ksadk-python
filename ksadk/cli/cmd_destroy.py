"""
agentengine destroy - 停止并销毁 Agent 实例
"""

import click
import asyncio
from pathlib import Path
from ksadk.cli.agent_ref import merge_agent_inputs, resolve_agent_ref
from ksadk.cli.dry_run import dry_run_option, run_async_with_dry_run, effective_dry_run
from ksadk.cli.error_utils import print_exception
from ksadk.deployment import DeploymentManager, DeployTarget
from ksadk.cli.ui import (
    print_error,
    print_info,
    print_kv,
    print_success,
    print_title,
    print_warn,
)


@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.argument("agent_ref", required=False)
@click.option("--agent", "--agent-id", "agent_option", "-a", help="Agent 名称或 ID")
@click.option("--force", "-f", is_flag=True, help="强制删除，不提示确认")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--account-id", envvar="KSYUN_ACCOUNT_ID", help="金山云账号 ID")
@dry_run_option()
def destroy(agent_ref: str, agent_option: str, force: bool, region: str, account_id: str, dry_run: bool):
    """停止并销毁 Agent 实例，释放相关资源

    \b
    示例:
        # 1) 目录内自动解析 agent
        agentengine destroy --account-id X-Ksc-Account-Id --force
        # 2) 显式指定 agent
        agentengine destroy --agent ar-xxxx --account-id X-Ksc-Account-Id --force
        # 3) 显式指定区域
        KSYUN_REGION=cn-beijing-6 agentengine destroy --agent ar-xxxx --account-id X-Ksc-Account-Id --dry-run
    """
    dry_run = effective_dry_run(dry_run)

    try:
        agent_input = merge_agent_inputs(
            agent_option=agent_option,
            positional_agent=agent_ref,
        )
    except ValueError as e:
        print_exception("错误", e)
        raise SystemExit(1)

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
    if resolved.source != "cli":
        print_info(f"未显式指定 Agent，使用 {resolved.source_text}: {resolved.value}")
    agent = resolved.value

    # 检查账号 ID
    if not account_id:
        print_error("错误: 需要金山云账号 ID")
        print_info("提示: 设置 KSYUN_ACCOUNT_ID 环境变量或使用 --account-id 参数")
        raise SystemExit(1)

    agent_id = agent
    if not dry_run:
        try:
            agent_id = asyncio.run(_resolve_agent_id(agent, region, account_id))
        except Exception as e:
            print_exception(f"错误: 无法解析 Agent '{agent}'", e)
            raise SystemExit(1)
        if agent_id != agent:
            print_info(f"已解析为 Agent ID: {agent_id}")

    # Dry Run 提示
    print_title("销毁 Agent")
    if dry_run:
        print_warn(f"[Dry Run] 准备销毁 Agent: {agent_id} (Region: {region})")
    else:
        print_warn(f"即将销毁 Agent: {agent_id}")
        print_kv("区域", region)

    if not force and not dry_run:
        if not click.confirm(f"确定要销毁 Agent '{agent_id}' 吗? 此操作不可恢复"):
            print_info("已取消")
            return

    # 构造 Provider 和 Target
    provider_name = "serverless"  # 目前默认 serverless
    
    try:
        provider = DeploymentManager.get_provider(provider_name)
    except ValueError as e:
        print_exception("错误", e)
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
        print_info("正在停止 Agent 实例...")

    # 调用 Provider 销毁
    try:
        success = run_async_with_dry_run(
            provider.destroy(agent_id, deploy_target),
            dry_run=dry_run,
        )

        if success:
            print_success("Agent 已销毁")
        else:
            if not dry_run:
                print_error("销毁失败，请检查错误信息")
                raise SystemExit(1)
    except Exception as e:
        print_exception("操作失败", e)
        raise SystemExit(1)


async def _resolve_agent_id(agent_ref: str, region: str, account_id: str) -> str:
    """将 Agent 引用（ID 或名称）解析为 Agent ID。"""
    from ksadk.api import AgentEngineClient

    extra_headers = {}
    if account_id:
        extra_headers["X-Ksc-Account-Id"] = account_id

    async with AgentEngineClient(region=region, extra_headers=extra_headers) as client:
        # 1) 先按 ID 查询
        try:
            response = await client.get_agent(agent_id=agent_ref)
            resolved = _extract_agent_id(response)
            if resolved:
                return resolved
        except Exception:
            pass

        # 2) 再按名称查询
        try:
            response = await client.get_agent(name=agent_ref)
            resolved = _extract_agent_id(response)
            if resolved:
                return resolved
        except Exception:
            pass

    raise ValueError("服务端未找到对应 Agent")


def _extract_agent_id(response: dict) -> str:
    basic = response.get("basic", {}) if isinstance(response, dict) else {}
    return basic.get("agent_id") or response.get("agent_id") or response.get("id") or ""
