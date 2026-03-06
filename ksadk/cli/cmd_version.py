"""
agentengine version - 版本管理命令组

子命令:
    version list      列出版本历史
    version release   发布新版本
    version rollback  回滚到指定版本
"""

import os
import click
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta, timezone

from ksadk.api.client import DryRunExit
from ksadk.cli.agent_ref import merge_agent_inputs, resolve_agent_ref
from ksadk.cli.dry_run import dry_run_option, run_async_with_dry_run, effective_dry_run
from ksadk.cli.ui import get_console, new_table, status_rich_style

console = get_console()


def _get_client(dry_run: bool = False):
    """获取 API 客户端"""
    from ksadk.api import AgentEngineClient

    access_key = os.getenv("KSYUN_ACCESS_KEY") or os.getenv("KS3_ACCESS_KEY")
    secret_key = os.getenv("KSYUN_SECRET_KEY") or os.getenv("KS3_SECRET_KEY")
    region = os.getenv("KSYUN_REGION", "cn-beijing-6")

    return AgentEngineClient(
        access_key=access_key,
        secret_key=secret_key,
        region=region,
        dry_run=dry_run,
    )


def _extract_agent_id(agent: dict) -> Optional[str]:
    if not isinstance(agent, dict):
        return None
    basic = agent.get("basic", {})
    if isinstance(basic, dict):
        agent_id = basic.get("agent_id")
        if agent_id:
            return agent_id
    return agent.get("agent_id") or agent.get("id")


async def _resolve_agent_id(agent_ref: str, client) -> Optional[str]:
    """按 ID/名称解析 Agent ID。"""
    # 1) 先按 ID 查询
    try:
        agent = await client.get_agent(agent_id=agent_ref)
        agent_id = _extract_agent_id(agent)
        if agent_id:
            return agent_id
    except DryRunExit:
        raise
    except Exception:
        pass

    # 2) 再按名称查询
    try:
        agent = await client.get_agent(name=agent_ref)
        agent_id = _extract_agent_id(agent)
        if agent_id:
            return agent_id
    except DryRunExit:
        raise
    except Exception:
        pass
    return None


async def _resolve_target_agent_id(
    *,
    agent_option: Optional[str],
    positional_agent: Optional[str],
    legacy_name: Optional[str],
    client,
) -> str:
    try:
        agent_input = merge_agent_inputs(
            agent_option=agent_option,
            positional_agent=positional_agent,
            legacy_name=legacy_name,
        )
    except ValueError as e:
        raise ValueError(str(e))

    resolved = resolve_agent_ref(
        agent_input,
        cwd=Path("."),
        include_state=True,
        include_project_config=True,
    )
    if not resolved:
        raise ValueError(
            "请指定 Agent（--agent 或位置参数），或在当前目录提供可解析的本地配置"
        )

    if resolved.source != "cli":
        console.print(
            f"[dim]ℹ 未显式指定 Agent，使用 {resolved.source_text}: {resolved.value}[/dim]"
        )

    agent_id = await _resolve_agent_id(resolved.value, client)
    if not agent_id:
        raise ValueError(f"未找到 Agent: {resolved.value}")
    return agent_id


@click.group("version")
def version():
    """版本管理命令

    \b
    示例:
        # 1) 目录内自动解析 agent
        agentengine version list
        # 2) 显式指定 agent
        agentengine version list --agent ar-xxxx
        # 3) 显式指定区域
        KSYUN_REGION=cn-beijing-6 agentengine version list --agent ar-xxxx

    \b
    说明:
        - 在项目目录下可不传 --agent，会自动从本地状态/配置解析目标 Agent
        - 也支持显式指定: --agent / --agent-id / 位置参数
        - 跨环境执行时请显式设置 KSYUN_REGION
    """
    pass


@version.command("list")
@click.argument("agent_ref", required=False)
@click.option("--agent", "--agent-id", "agent_option", "-a", help="Agent 名称或 ID")
@click.option("--name", "-n", hidden=True, help="(兼容) Agent 名称")
@click.option("--page", "-p", default=1, help="页码")
@click.option("--size", "-s", default=10, help="每页数量")
@dry_run_option()
def list_versions(
    agent_ref: Optional[str],
    agent_option: Optional[str],
    name: Optional[str],
    page: int,
    size: int,
    dry_run: bool,
):
    """列出版本历史

    \b
    示例:
        # 1) 目录内自动解析 agent
        agentengine version list
        # 2) 显式指定 agent
        agentengine version list --agent ar-xxxx
        # 3) 显式指定区域
        KSYUN_REGION=cn-beijing-6 agentengine version list --agent ar-xxxx
    """
    dry_run = effective_dry_run(dry_run)
    run_async_with_dry_run(
        _list_versions_async(agent_ref, agent_option, name, page, size, dry_run),
        dry_run=dry_run,
    )


async def _list_versions_async(
    agent_ref: Optional[str],
    agent_option: Optional[str],
    name: Optional[str],
    page: int,
    size: int,
    dry_run: bool,
):
    client = _get_client(dry_run=dry_run)

    try:
        agent_id = await _resolve_target_agent_id(
            agent_option=agent_option,
            positional_agent=agent_ref,
            legacy_name=name,
            client=client,
        )

        result = await client.list_versions(agent_id, page, size)
        versions = result.get("versions", [])
        total = result.get("total", 0)

        if not versions:
            console.print("[yellow]No versions found[/yellow]")
            return

        # 创建表格
        table = new_table(f"版本列表  [muted](总计: {total})[/]")

        table.add_column("版本 (Tag)", style="green")
        table.add_column("状态", style="yellow")
        table.add_column("流量", style="blue")
        table.add_column("创建时间", style="dim")
        table.add_column("描述", style="white", max_width=50)

        for v in versions:
            # _action 已统一转为 snake_case
            raw_status = v.get("status") or ""
            is_current = raw_status.lower() == "current"
            status = "CURRENT" if is_current else "HISTORY"
            status_style = status_rich_style("RUNNING") if is_current else "muted"

            # 时间转换 (UTC -> Beijing)
            created_at = v.get("created_at")
            if created_at:
                try:
                    dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    created_at = dt.astimezone(timezone(timedelta(hours=8))).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                except Exception:
                    created_at = created_at[:19]
            else:
                created_at = "-"

            # 描述处理
            desc = v.get("description") or ""
            if "Auto-released by" in desc:
                desc = desc.split(" at ")[0]
                desc = desc.replace("Auto-released by deploy", "部署自动发布")
                desc = desc.replace("Auto-released by launch", "Launch自动发布")

            traffic = v.get("traffic_percentage") or 0

            table.add_row(
                v.get("tag") or "-",
                f"[{status_style}]{status}[/{status_style}]",
                f"{traffic}%",
                created_at,
                desc.strip(),
            )

        console.print(table)

    except DryRunExit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Error: {e}[/red]")
    finally:
        await client.close()


@version.command("release")
@click.argument("agent_ref", required=False)
@click.option("--agent", "--agent-id", "agent_option", "-a", help="Agent 名称或 ID")
@click.option("--name", "-n", hidden=True, help="(兼容) Agent 名称")
@click.option("--tag", "-t", help="版本标签 (不填则自动生成)")
@click.option("--description", "-d", help="版本描述")
@dry_run_option()
def release_version(
    agent_ref: Optional[str],
    agent_option: Optional[str],
    name: Optional[str],
    tag: Optional[str],
    description: Optional[str],
    dry_run: bool,
):
    """发布新版本

    \b
    创建当前 Agent 配置的版本快照，并设为当前版本。

    \b
    示例:
        # 1) 目录内自动解析 agent
        agentengine version release --tag v1.0.1 --description "release note"
        # 2) 显式指定 agent
        agentengine version release --agent ar-xxxx --tag v1.0.1 --description "release note"
        # 3) 显式指定区域
        KSYUN_REGION=cn-beijing-6 agentengine version release --agent ar-xxxx --tag v1.0.1
    """
    dry_run = effective_dry_run(dry_run)
    run_async_with_dry_run(
        _release_version_async(agent_ref, agent_option, name, tag, description, dry_run),
        dry_run=dry_run,
    )


async def _release_version_async(
    agent_ref: Optional[str],
    agent_option: Optional[str],
    name: Optional[str],
    tag: Optional[str],
    description: Optional[str],
    dry_run: bool,
):
    client = _get_client(dry_run=dry_run)

    try:
        agent_id = await _resolve_target_agent_id(
            agent_option=agent_option,
            positional_agent=agent_ref,
            legacy_name=name,
            client=client,
        )

        with console.status("[bold blue]Releasing version...[/bold blue]"):
            result = await client.release_version(agent_id, tag, description)

        console.print(f"[green]✓ Version released successfully[/green]")
        console.print(f"  Tag: [cyan]{result.get('tag')}[/cyan]")
        console.print(f"  Version ID: [dim]{result.get('id')}[/dim]")
        console.print(f"  Artifact: [dim]{result.get('artifact_path')}[/dim]")

    except DryRunExit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Error: {e}[/red]")
    finally:
        await client.close()


@version.command("rollback")
@click.argument("agent_ref", required=False)
@click.option("--agent", "--agent-id", "agent_option", "-a", help="Agent 名称或 ID")
@click.option("--name", "-n", hidden=True, help="(兼容) Agent 名称")
@click.option("--to", "target", required=True, help="目标版本 (tag 或 version ID)")
@click.option("--yes", "-y", is_flag=True, help="跳过确认")
@dry_run_option()
def rollback_version(
    agent_ref: Optional[str],
    agent_option: Optional[str],
    name: Optional[str],
    target: str,
    yes: bool,
    dry_run: bool,
):
    """回滚到指定版本

    \b
    将 Agent 回滚到指定的历史版本。

    \b
    示例:
        # 1) 目录内自动解析 agent
        agentengine version rollback --to v1.0.0 -y
        # 2) 显式指定 agent
        agentengine version rollback --agent ar-xxxx --to v1.0.0 -y
        # 3) 显式指定区域
        KSYUN_REGION=cn-beijing-6 agentengine version rollback --agent ar-xxxx --to v1.0.0 -y
    """
    dry_run = effective_dry_run(dry_run)
    run_async_with_dry_run(
        _rollback_version_async(agent_ref, agent_option, name, target, yes, dry_run),
        dry_run=dry_run,
    )


async def _rollback_version_async(
    agent_ref: Optional[str],
    agent_option: Optional[str],
    name: Optional[str],
    target: str,
    yes: bool,
    dry_run: bool,
):
    client = _get_client(dry_run=dry_run)

    try:
        agent_id = await _resolve_target_agent_id(
            agent_option=agent_option,
            positional_agent=agent_ref,
            legacy_name=name,
            client=client,
        )

        # 确认操作
        if not yes and not dry_run:
            console.print(f"[yellow]⚠ This will rollback Agent to version: {target}[/yellow]")
            console.print("[yellow]  The agent may be briefly unavailable during rollback.[/yellow]")
            if not click.confirm("Do you want to continue?"):
                console.print("[dim]Cancelled[/dim]")
                return

        # 获取 KS3 凭证 (用于 Serverless 更新配置)
        access_key = os.getenv("KSYUN_ACCESS_KEY") or os.getenv("KS3_ACCESS_KEY")
        secret_key = os.getenv("KSYUN_SECRET_KEY") or os.getenv("KS3_SECRET_KEY")

        # 判断 target 是 tag 还是 version_id
        # UUID 格式: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
        is_uuid = len(target) == 36 and target.count("-") == 4

        with console.status("[bold blue]Rolling back...[/bold blue]"):
            if is_uuid:
                result = await client.rollback_version(
                    agent_id,
                    target_version_id=target,
                    ks3_access_key=access_key,
                    ks3_secret_key=secret_key,
                )
            else:
                result = await client.rollback_version(
                    agent_id,
                    target_tag=target,
                    ks3_access_key=access_key,
                    ks3_secret_key=secret_key,
                )

        console.print(f"[green]✓ Rollback successful[/green]")
        console.print(f"  Current Version: [cyan]{result.get('current_tag')}[/cyan]")
        if result.get("message"):
            console.print(f"  {result.get('message')}")

    except DryRunExit:
        raise
    except Exception as e:
        console.print(f"[red]✗ Error: {e}[/red]")
    finally:
        await client.close()
