"""agentengine version - Agent 版本资源管理。"""

import os
import click
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta, timezone

from ksadk.api.client import DryRunExit
from ksadk.cli.agent_ref import merge_agent_inputs, resolve_agent_ref
from ksadk.cli.dry_run import dry_run_option, run_async_with_dry_run, effective_dry_run
from ksadk.cli.error_utils import abort_with_cli_error, resolution_error
from ksadk.cli.resource_common import (
    CONTEXT_SETTINGS,
    ResourceActionSet,
    ResourceDescriptor,
    ResourceListSchema,
    ResourceStatusSchema,
    build_resource_group_help,
    confirm_destructive,
    confirm_options,
    pagination_options,
    render_descriptor_list,
    render_descriptor_status,
)
from ksadk.cli.ui import get_console, output_option as cli_output_option, status_rich_style

console = get_console()

VERSION_RESOURCE = ResourceDescriptor(
    name="版本",
    summary="Agent 版本资源管理。",
    resource_key="version",
    actions=ResourceActionSet(
        list="agentengine version list [agent_ref]",
        extra=("release", "rollback"),
    ),
    list_schema=ResourceListSchema(
        title="版本列表",
        noun="版本",
        columns=(
            {"header": "Tag", "key": "tag", "style": "green"},
            {"header": "状态", "key": "status", "style": "yellow"},
            {"header": "流量", "key": "traffic", "style": "blue"},
            {"header": "创建时间", "key": "created_at", "style": "dim"},
            {"header": "描述", "key": "description", "style": "white", "max_width": 50},
        ),
        empty_message="没有找到版本记录",
        summary_lines=("使用 `agentengine version release` 创建新版本。",),
    ),
    status_schema=ResourceStatusSchema(
        title="版本结果",
        next_steps=("agentengine version list", "agentengine agent status"),
    ),
    examples=(
        "agentengine version list",
        "agentengine version list --agent ar-xxxx",
        "agentengine version release --agent ar-xxxx --tag v1.0.1",
        "agentengine version rollback --agent ar-xxxx --to v1.0.0 -y",
    ),
    notes=(
        "在项目目录下可不传 --agent，会自动从本地状态/配置解析目标 Agent",
        "也支持显式指定: --agent / --agent-id / 位置参数",
        "跨环境执行时请显式设置 KSYUN_REGION",
    ),
    missing_ref_message="请指定 Agent（--agent 或位置参数），或在当前目录提供可解析的本地配置",
    resolution_commands=("agentengine agent list",),
    list_action_help="列出版本历史",
    extra_action_help=(
        ("release", "发布新版本"),
        ("rollback", "回滚到指定版本"),
    ),
)


def _get_client(*, region: str, dry_run: bool = False):
    """获取 API 客户端"""
    from ksadk.api import AgentEngineClient

    access_key = os.getenv("KSYUN_ACCESS_KEY") or os.getenv("KS3_ACCESS_KEY")
    secret_key = os.getenv("KSYUN_SECRET_KEY") or os.getenv("KS3_SECRET_KEY")
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


@click.group("version", context_settings=CONTEXT_SETTINGS, help=build_resource_group_help(VERSION_RESOURCE))
def version():
    pass


def _abort_version_error(
    err: Exception,
    *,
    context: str | None = None,
    argv: list[str] | None = None,
) -> None:
    abort_with_cli_error(err, context=context, argv=argv)


@version.command("list", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@click.option("--agent", "--agent-id", "agent_option", "-a", help="Agent 名称或 ID")
@click.option("--name", "-n", hidden=True, help="(兼容) Agent 名称")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@pagination_options(default_page=1, default_size=20)
@dry_run_option()
@cli_output_option()
def list_versions(
    agent_ref: Optional[str],
    agent_option: Optional[str],
    name: Optional[str],
    region: str,
    page: int,
    size: int,
    dry_run: bool,
    output_mode: str | None,
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
    try:
        run_async_with_dry_run(
            _list_versions_async(agent_ref, agent_option, name, region, page, size, dry_run),
            dry_run=dry_run,
            dry_run_resource="version",
            dry_run_action="list",
        )
    except Exception as e:
        _abort_version_error(e, context="获取版本列表失败", argv=["version", "list"])


async def _list_versions_async(
    agent_ref: Optional[str],
    agent_option: Optional[str],
    name: Optional[str],
    region: str,
    page: int,
    size: int,
    dry_run: bool,
):
    client = _get_client(region=region, dry_run=dry_run)

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
        rows = []
        items = []

        for v in versions:
            # _action 已统一转为 snake_case
            raw_status = v.get("status") or ""
            is_current = raw_status.lower() == "current"
            status = "当前" if is_current else "历史"
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

            rows.append(
                (
                    str(v.get("tag") or "-"),
                    f"[{status_style}]{status}[/{status_style}]",
                    f"{traffic}%",
                    created_at,
                    desc.strip(),
                )
            )
            items.append(
                {
                    "tag": str(v.get("tag") or "-"),
                    "status": status,
                    "traffic": f"{traffic}%",
                    "created_at": created_at,
                    "description": desc.strip(),
                }
            )

        render_descriptor_list(
            VERSION_RESOURCE,
            rows=rows,
            total=int(total or len(versions)),
            page=page,
            size=size,
            items=items,
        )

    except DryRunExit:
        raise
    except ValueError as e:
        raise resolution_error(str(e), hints=list(VERSION_RESOURCE.resolution_commands))
    finally:
        await client.close()


@version.command("release", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@click.option("--agent", "--agent-id", "agent_option", "-a", help="Agent 名称或 ID")
@click.option("--name", "-n", hidden=True, help="(兼容) Agent 名称")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--tag", "-t", help="版本标签 (不填则自动生成)")
@click.option("--description", "-d", help="版本描述")
@dry_run_option()
@cli_output_option()
def release_version(
    agent_ref: Optional[str],
    agent_option: Optional[str],
    name: Optional[str],
    region: str,
    tag: Optional[str],
    description: Optional[str],
    dry_run: bool,
    output_mode: str | None,
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
    try:
        run_async_with_dry_run(
            _release_version_async(agent_ref, agent_option, name, region, tag, description, dry_run),
            dry_run=dry_run,
            dry_run_resource="version",
            dry_run_action="release",
        )
    except Exception as e:
        _abort_version_error(e, context="版本发布失败", argv=["version", "release"])


async def _release_version_async(
    agent_ref: Optional[str],
    agent_option: Optional[str],
    name: Optional[str],
    region: str,
    tag: Optional[str],
    description: Optional[str],
    dry_run: bool,
):
    client = _get_client(region=region, dry_run=dry_run)

    try:
        agent_id = await _resolve_target_agent_id(
            agent_option=agent_option,
            positional_agent=agent_ref,
            legacy_name=name,
            client=client,
        )

        with console.status("[bold blue]正在发布版本...[/bold blue]"):
            result = await client.release_version(agent_id, tag, description)

        render_descriptor_status(
            VERSION_RESOURCE,
            title="版本发布结果",
            subtitle=str(result.get("tag") or "latest"),
            fields=[
                ("Tag", str(result.get("tag") or "-"), "#58a6ff"),
                ("ID", str(result.get("id") or "-"), None),
                ("Artifact", str(result.get("artifact_path") or "-"), None),
            ],
            action="release",
            item={
                "tag": str(result.get("tag") or "-"),
                "id": str(result.get("id") or "-"),
                "artifact_path": str(result.get("artifact_path") or "-"),
            },
        )

    except DryRunExit:
        raise
    except ValueError as e:
        raise resolution_error(str(e), hints=list(VERSION_RESOURCE.resolution_commands))
    finally:
        await client.close()


@version.command("rollback", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@click.option("--agent", "--agent-id", "agent_option", "-a", help="Agent 名称或 ID")
@click.option("--name", "-n", hidden=True, help="(兼容) Agent 名称")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--to", "target", required=True, help="目标版本 (tag 或 version ID)")
@confirm_options()
@dry_run_option()
@cli_output_option()
def rollback_version(
    agent_ref: Optional[str],
    agent_option: Optional[str],
    name: Optional[str],
    region: str,
    target: str,
    assume_yes: bool,
    dry_run: bool,
    output_mode: str | None,
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
    try:
        run_async_with_dry_run(
            _rollback_version_async(agent_ref, agent_option, name, region, target, assume_yes, dry_run),
            dry_run=dry_run,
            dry_run_resource="version",
            dry_run_action="rollback",
        )
    except Exception as e:
        _abort_version_error(e, context="版本回滚失败", argv=["version", "rollback"])


async def _rollback_version_async(
    agent_ref: Optional[str],
    agent_option: Optional[str],
    name: Optional[str],
    region: str,
    target: str,
    assume_yes: bool,
    dry_run: bool,
):
    client = _get_client(region=region, dry_run=dry_run)

    try:
        agent_id = await _resolve_target_agent_id(
            agent_option=agent_option,
            positional_agent=agent_ref,
            legacy_name=name,
            client=client,
        )

        if not confirm_destructive(
            assume_yes=assume_yes,
            dry_run=dry_run,
            prompt=f"即将把 Agent 回滚到版本 {target}，回滚期间 Agent 可能会短暂不可用。是否继续？",
        ):
            return

        # 获取 KS3 凭证 (用于 Serverless 更新配置)
        access_key = os.getenv("KSYUN_ACCESS_KEY") or os.getenv("KS3_ACCESS_KEY")
        secret_key = os.getenv("KSYUN_SECRET_KEY") or os.getenv("KS3_SECRET_KEY")

        # 判断 target 是 tag 还是 version_id
        # UUID 格式: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
        is_uuid = len(target) == 36 and target.count("-") == 4

        with console.status("[bold blue]正在回滚版本...[/bold blue]"):
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

        fields = [
            ("当前版本", str(result.get("current_tag") or "-"), "#58a6ff"),
        ]
        if result.get("message"):
            fields.append(("消息", str(result.get("message")), None))
        render_descriptor_status(
            VERSION_RESOURCE,
            title="版本回滚结果",
            subtitle=str(target),
            fields=fields,
            action="rollback",
            item={
                "target": str(target),
                "current_tag": str(result.get("current_tag") or "-"),
                "message": str(result.get("message") or ""),
            },
        )

    except DryRunExit:
        raise
    except ValueError as e:
        raise resolution_error(str(e), hints=list(VERSION_RESOURCE.resolution_commands))
    finally:
        await client.close()
