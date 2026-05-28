"""
agentengine delete - 删除 Agent 实例
"""

import click
import asyncio
from pathlib import Path
from ksadk.cli.agent_ref import resolve_agent_ref
from ksadk.cli.dry_run import dry_run_option, run_async_with_dry_run, effective_dry_run
from ksadk.cli.error_utils import abort_with_cli_error, remote_error, resolution_error, usage_error, validation_error
from ksadk.cli.resource_common import (
    CONTEXT_SETTINGS,
    CompatibilityAliasCommand,
    confirm_destructive,
    print_compatibility_hint,
    render_resource_status,
)
from ksadk.deployment import DeploymentManager, DeployTarget
from ksadk.cli.ui import (
    is_json_output,
    print_error,
    print_info,
    print_kv,
    print_success,
    print_title,
    print_warn,
)


def _destroy_impl(
    *,
    agent_refs: tuple[str, ...],
    agent_options: tuple[str, ...],
    assume_yes: bool,
    region: str,
    account_id: str,
    dry_run: bool,
):
    """停止并销毁 Agent 实例，释放相关资源

    \b
    示例:
        # 1) 目录内自动解析 agent
        agentengine delete --account-id X-Ksc-Account-Id --yes
        # 2) 显式指定 agent
        agentengine delete --agent ar-xxxx --account-id X-Ksc-Account-Id --force
        # 3) 显式指定区域
        KSYUN_REGION=cn-beijing-6 agentengine delete --agent ar-xxxx --account-id X-Ksc-Account-Id --dry-run
        # 4) 批量删除
        agentengine delete ar-xxxx ar-yyyy --account-id X-Ksc-Account-Id --yes
    """
    dry_run = effective_dry_run(dry_run)
    agents = _collect_agent_refs(agent_refs=agent_refs, agent_options=agent_options)

    # 检查账号 ID
    if not account_id:
        raise validation_error(
            "需要金山云账号 ID",
            hints=["设置 KSYUN_ACCOUNT_ID 环境变量或使用 --account-id 参数。"],
        )

    resolved_agent_ids = agents
    if not dry_run:
        try:
            resolved_agent_ids = asyncio.run(_resolve_agent_ids(agents, region, account_id))
        except Exception as e:
            raise resolution_error(f"无法解析 Agent: {e}")

    # Dry Run 提示
    title = "批量删除 Agent" if len(resolved_agent_ids) > 1 else "删除 Agent"
    print_title(title)
    if dry_run:
        print_warn(f"[Dry Run] 准备删除 {len(resolved_agent_ids)} 个 Agent (Region: {region})")
        for agent_id in resolved_agent_ids:
            print_info(f"  - {agent_id}")
    else:
        print_warn(f"即将删除 {len(resolved_agent_ids)} 个 Agent")
        print_kv("区域", region)
        for agent_id in resolved_agent_ids:
            print_info(f"  - {agent_id}")

    if not confirm_destructive(
        assume_yes=assume_yes,
        dry_run=dry_run,
        prompt=f"确定要删除 Agent '{'、'.join(resolved_agent_ids)}' 吗? 此操作不可恢复",
    ):
        return

    # 构造 Provider 和 Target
    provider_name = "serverless"  # 目前默认 serverless
    
    try:
        provider = DeploymentManager.get_provider(provider_name)
    except ValueError as e:
        raise usage_error(str(e))

    deploy_target = DeployTarget(
        provider=provider_name,
        region=region,
        extra={
            "account_id": account_id,
            "dry_run": dry_run,
            "project_dir": str(Path(".").resolve()),
        }
    )

    if not dry_run:
        print_info("正在停止并删除 Agent 实例...")

    # 调用 Provider 销毁
    failed_agents: list[str] = []
    failure_reasons: dict[str, str] = {}
    deleted_agents: list[str] = []
    for agent_id in resolved_agent_ids:
        try:
            success = run_async_with_dry_run(
                provider.destroy(agent_id, deploy_target),
                dry_run=dry_run,
                dry_run_resource="agent",
                dry_run_action="delete",
            )
        except Exception as e:
            failed_agents.append(agent_id)
            failure_reasons[agent_id] = str(e)
            if not dry_run:
                print_error(f"Agent 删除失败: {agent_id} ({e})")
            continue

        if success:
            deleted_agents.append(agent_id)
            print_success(f"Agent 已删除: {agent_id}")
        elif not dry_run:
            failed_agents.append(agent_id)
            failure_reasons[agent_id] = "provider returned False"

    if failed_agents and not dry_run and not is_json_output():
        print_error(f"以下 Agent 删除失败: {', '.join(failed_agents)}")

    if failed_agents and not dry_run and is_json_output():
        abort_with_cli_error(
            remote_error(
                f"以下 Agent 删除失败: {', '.join(failed_agents)}",
                details={
                    "targets": list(resolved_agent_ids),
                    "deleted": deleted_agents,
                    "failed": failed_agents,
                    "errors": failure_reasons,
                    "region": region,
                },
                hints=["请先执行 `agentengine agent status --agent <agent_id>` 查看当前状态。"],
            )
        )

    render_resource_status(
        title="Agent 删除结果",
        subtitle="批量删除" if len(resolved_agent_ids) > 1 else (resolved_agent_ids[0] if resolved_agent_ids else "-"),
        fields=[
            ("目标数量", str(len(resolved_agent_ids)), None),
            ("已删除", ", ".join(deleted_agents) or "-", None),
            ("失败", ", ".join(failed_agents) or "-", None),
        ],
        resource="agent",
        action="delete",
        item={
            "targets": list(resolved_agent_ids),
            "deleted": deleted_agents,
            "failed": failed_agents,
            "errors": failure_reasons,
            "region": region,
        },
    )

    if failed_agents and not dry_run:
        abort_with_cli_error(
            remote_error(
                f"以下 Agent 删除失败: {', '.join(failed_agents)}",
                details={
                    "targets": list(resolved_agent_ids),
                    "deleted": deleted_agents,
                    "failed": failed_agents,
                    "errors": failure_reasons,
                    "region": region,
                },
                hints=["请先执行 `agentengine agent status --agent <agent_id>` 查看当前状态。"],
            )
        )


def run_delete_command(
    *,
    agent_refs: tuple[str, ...],
    agent_options: tuple[str, ...],
    assume_yes: bool,
    region: str,
    account_id: str | None,
    dry_run: bool,
    compatibility_alias: bool = False,
):
    if compatibility_alias:
        print_compatibility_hint(
            legacy="agentengine delete",
            canonical="agentengine agent delete",
        )
    _destroy_impl(
        agent_refs=agent_refs,
        agent_options=agent_options,
        assume_yes=assume_yes,
        region=region,
        account_id=account_id,
        dry_run=dry_run,
    )


@click.command(
    "destroy",
    context_settings=CONTEXT_SETTINGS,
    hidden=True,
    cls=CompatibilityAliasCommand,
    canonical_command="agentengine agent delete",
)
@click.argument("agent_refs", nargs=-1)
@click.option("--agent", "--agent-id", "agent_options", "-a", multiple=True, help="Agent 名称或 ID，可重复传入")
@click.option("--force", "-f", "assume_yes", is_flag=True, help="跳过确认")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="跳过确认")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--account-id", envvar="KSYUN_ACCOUNT_ID", help="金山云账号 ID")
@dry_run_option()
def destroy(
    agent_refs: tuple[str, ...],
    agent_options: tuple[str, ...],
    assume_yes: bool,
    region: str,
    account_id: str,
    dry_run: bool,
):
    run_delete_command(
        agent_refs=agent_refs,
        agent_options=agent_options,
        assume_yes=assume_yes,
        region=region,
        account_id=account_id,
        dry_run=dry_run,
        compatibility_alias=True,
    )


@click.command(
    "delete",
    context_settings=CONTEXT_SETTINGS,
    hidden=True,
    cls=CompatibilityAliasCommand,
    canonical_command="agentengine agent delete",
)
@click.argument("agent_refs", nargs=-1)
@click.option("--agent", "--agent-id", "agent_options", "-a", multiple=True, help="Agent 名称或 ID，可重复传入")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="跳过确认")
@click.option("--force", "-f", "assume_yes", is_flag=True, help="跳过确认")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--account-id", envvar="KSYUN_ACCOUNT_ID", help="金山云账号 ID")
@dry_run_option()
def delete(
    agent_refs: tuple[str, ...],
    agent_options: tuple[str, ...],
    assume_yes: bool,
    region: str,
    account_id: str,
    dry_run: bool,
):
    """删除 Agent。"""
    run_delete_command(
        agent_refs=agent_refs,
        agent_options=agent_options,
        assume_yes=assume_yes,
        region=region,
        account_id=account_id,
        dry_run=dry_run,
        compatibility_alias=True,
    )


def _collect_agent_refs(*, agent_refs: tuple[str, ...], agent_options: tuple[str, ...]) -> list[str]:
    collected = []
    for value in [*agent_options, *agent_refs]:
        normalized = str(value).strip()
        if normalized and normalized not in collected:
            collected.append(normalized)

    if collected:
        return collected

    resolved = resolve_agent_ref(
        None,
        cwd=Path("."),
        include_state=True,
        include_project_config=True,
    )
    if not resolved:
        raise resolution_error(
            "请指定 Agent（--agent 或位置参数），或在当前目录提供可解析的本地配置",
            hints=["自动解析顺序: .agentengine.state -> agentengine.yaml/ksadk.yaml", "请先执行 `agentengine agent list`。"],
        )

    if resolved.source != "cli":
        print_info(f"未显式指定 Agent，使用 {resolved.source_text}: {resolved.value}")
    return [resolved.value]


async def _resolve_agent_ids(agent_refs: list[str], region: str, account_id: str) -> list[str]:
    resolved_agent_ids = []
    for agent_ref in agent_refs:
        resolved_id = await _resolve_agent_id(agent_ref, region, account_id)
        if resolved_id != agent_ref:
            print_info(f"已解析 {agent_ref} -> {resolved_id}")
        resolved_agent_ids.append(resolved_id)
    return resolved_agent_ids


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
