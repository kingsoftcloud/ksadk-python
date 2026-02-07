"""
agentengine version - 版本管理命令组

子命令:
    version list      列出版本历史
    version release   发布新版本
    version rollback  回滚到指定版本
"""

import os
import asyncio
import click
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta, timezone

from rich.console import Console
from rich.table import Table
from rich import box


console = Console()


def _get_client():
    """获取 API 客户端"""
    from ksadk.api import AgentEngineClient
    
    access_key = os.getenv("KSYUN_ACCESS_KEY") or os.getenv("KS3_ACCESS_KEY")
    secret_key = os.getenv("KSYUN_SECRET_KEY") or os.getenv("KS3_SECRET_KEY")
    region = os.getenv("KSYUN_REGION", "cn-beijing-6")
    
    return AgentEngineClient(
        access_key=access_key,
        secret_key=secret_key,
        region=region,
    )


def _load_agent_config(agent_dir: Path) -> Optional[dict]:
    """加载 Agent 配置获取名称"""
    import json
    config_file = agent_dir / "agent.json"
    if config_file.exists():
        with open(config_file) as f:
            return json.load(f)
    return None


async def _get_agent_id(name: str, client) -> Optional[str]:
    """按名称获取 Agent ID"""
    agent = await client.get_agent_by_name(name)
    if agent:
        return agent.get("id")
    return None


@click.group("version")
def version():
    """版本管理命令"""
    pass


@version.command("list")
@click.option("--agent-id", "-i", help="Agent ID (如果不指定，尝试从当前目录的 agent.json 读取)")
@click.option("--name", "-n", help="Agent 名称 (用于查询 Agent ID)")
@click.option("--page", "-p", default=1, help="页码")
@click.option("--size", "-s", default=10, help="每页数量")
def list_versions(agent_id: Optional[str], name: Optional[str], page: int, size: int):
    """列出版本历史
    
    \b
    示例:
        agentengine version list --agent-id ag-xxxx
        agentengine version list --name my-agent
        agentengine version list   # 从当前目录读取 agent.json
    """
    asyncio.run(_list_versions_async(agent_id, name, page, size))


async def _list_versions_async(agent_id: Optional[str], name: Optional[str], page: int, size: int):
    client = _get_client()
    
    try:
        # 解析 Agent ID
        if not agent_id:
            if name:
                agent_id = await _get_agent_id(name, client)
                if not agent_id:
                    console.print(f"[red]✗ Agent '{name}' not found[/red]")
                    return
            else:
                # 尝试从当前目录读取
                config = _load_agent_config(Path.cwd())
                if config and config.get("name"):
                    agent_id = await _get_agent_id(config["name"], client)
                    if not agent_id:
                        console.print(f"[yellow]⚠ Agent '{config['name']}' not deployed yet[/yellow]")
                        return
                else:
                    console.print("[red]✗ Please specify --agent-id or --name[/red]")
                    return
        
        result = await client.list_versions(agent_id, page, size)
        versions = result.get("Versions", [])
        total = result.get("Total", 0)
        
        if not versions:
            console.print("[yellow]No versions found[/yellow]")
            return
        
        # 创建表格
        table = Table(
            title=f"版本列表 (总计: {total})",
            box=box.ROUNDED,
            show_header=True,
            header_style="bold cyan"
        )
        
        table.add_column("版本 (Tag)", style="green")
        table.add_column("状态", style="yellow")
        table.add_column("类型", style="blue")
        table.add_column("创建时间", style="dim")
        table.add_column("描述", style="white", max_width=50)
        
        for v in versions:
            status = "✓ 当前" if v.get("is_current") else "历史"
            status_style = "bold green" if v.get("is_current") else "dim"
            
            # 时间转换 (UTC -> Beijing)
            created_at = v.get("created_at")
            if created_at:
                try:
                    # 兼容 Python 3.7+ fromisoformat
                    dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    created_at = dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    # Fallback
                    created_at = created_at[:19]
            else:
                created_at = "-"

            # 描述处理：移除时间戳后缀，汉化旧数据
            desc = v.get("description") or ""
            if "Auto-released by" in desc:
                # 移除 at 2026-xx-xx ...
                desc = desc.split(" at ")[0]
                desc = desc.replace("Auto-released by deploy", "部署自动发布")
                desc = desc.replace("Auto-released by launch", "Launch自动发布")

            table.add_row(
                v.get("tag", "-"),
                f"[{status_style}]{status}[/{status_style}]",
                v.get("artifact_type", "-"),
                created_at,
                desc.strip(),
            )
        
        console.print(table)
        
    except Exception as e:
        console.print(f"[red]✗ Error: {e}[/red]")
    finally:
        await client.close()


@version.command("release")
@click.option("--agent-id", "-i", help="Agent ID")
@click.option("--name", "-n", help="Agent 名称")
@click.option("--tag", "-t", help="版本标签 (不填则自动生成)")
@click.option("--description", "-d", help="版本描述")
def release_version(agent_id: Optional[str], name: Optional[str], tag: Optional[str], description: Optional[str]):
    """发布新版本
    
    \b
    创建当前 Agent 配置的版本快照，并设为当前版本。
    
    \b
    示例:
        agentengine version release --tag v1.0.0 --description "初始版本"
        agentengine version release   # 自动生成 tag
    """
    asyncio.run(_release_version_async(agent_id, name, tag, description))


async def _release_version_async(
    agent_id: Optional[str], 
    name: Optional[str], 
    tag: Optional[str], 
    description: Optional[str]
):
    client = _get_client()
    
    try:
        # 解析 Agent ID
        if not agent_id:
            if name:
                agent_id = await _get_agent_id(name, client)
                if not agent_id:
                    console.print(f"[red]✗ Agent '{name}' not found[/red]")
                    return
            else:
                config = _load_agent_config(Path.cwd())
                if config and config.get("name"):
                    name = config["name"]
                    agent_id = await _get_agent_id(name, client)
                    if not agent_id:
                        console.print(f"[yellow]⚠ Agent '{name}' not deployed yet[/yellow]")
                        return
                else:
                    console.print("[red]✗ Please specify --agent-id or --name[/red]")
                    return
        
        with console.status("[bold blue]Releasing version...[/bold blue]"):
            result = await client.release_version(agent_id, tag, description)
        
        console.print(f"[green]✓ Version released successfully[/green]")
        console.print(f"  Tag: [cyan]{result.get('tag')}[/cyan]")
        console.print(f"  Version ID: [dim]{result.get('id')}[/dim]")
        console.print(f"  Artifact: [dim]{result.get('artifact_path')}[/dim]")
        
    except Exception as e:
        console.print(f"[red]✗ Error: {e}[/red]")
    finally:
        await client.close()


@version.command("rollback")
@click.option("--agent-id", "-i", help="Agent ID")
@click.option("--name", "-n", help="Agent 名称")
@click.option("--to", "target", required=True, help="目标版本 (tag 或 version ID)")
@click.option("--yes", "-y", is_flag=True, help="跳过确认")
def rollback_version(agent_id: Optional[str], name: Optional[str], target: str, yes: bool):
    """回滚到指定版本
    
    \b
    将 Agent 回滚到指定的历史版本。
    
    \b
    示例:
        agentengine version rollback --to v1.0.0
        agentengine version rollback --to version-uuid-xxx
        agentengine version rollback --to v1.0.0 -y  # 跳过确认
    """
    asyncio.run(_rollback_version_async(agent_id, name, target, yes))


async def _rollback_version_async(
    agent_id: Optional[str], 
    name: Optional[str], 
    target: str, 
    yes: bool
):
    client = _get_client()
    
    try:
        # 解析 Agent ID
        if not agent_id:
            if name:
                agent_id = await _get_agent_id(name, client)
                if not agent_id:
                    console.print(f"[red]✗ Agent '{name}' not found[/red]")
                    return
            else:
                config = _load_agent_config(Path.cwd())
                if config and config.get("name"):
                    name = config["name"]
                    agent_id = await _get_agent_id(name, client)
                    if not agent_id:
                        console.print(f"[yellow]⚠ Agent '{name}' not deployed yet[/yellow]")
                        return
                else:
                    console.print("[red]✗ Please specify --agent-id or --name[/red]")
                    return
        
        # 确认操作
        if not yes:
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
                    ks3_secret_key=secret_key
                )
            else:
                result = await client.rollback_version(
                    agent_id, 
                    target_tag=target,
                    ks3_access_key=access_key,
                    ks3_secret_key=secret_key
                )
        
        console.print(f"[green]✓ Rollback successful[/green]")
        console.print(f"  Current Version: [cyan]{result.get('current_tag')}[/cyan]")
        if result.get('message'):
            console.print(f"  {result.get('message')}")
        
    except Exception as e:
        console.print(f"[red]✗ Error: {e}[/red]")
    finally:
        await client.close()
