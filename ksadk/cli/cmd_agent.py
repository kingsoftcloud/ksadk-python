"""Canonical Agent resource group."""

from __future__ import annotations

import click
from pathlib import Path

from ksadk.cli.cmd_destroy import run_delete_command
from ksadk.cli.cmd_invoke import run_invoke_command
from ksadk.cli.cmd_status import run_status_command
from ksadk.cli.dry_run import dry_run_option
from ksadk.cli.error_utils import ensure_dry_run_supported, ensure_json_output_supported
from ksadk.cli.resource_common import CONTEXT_SETTINGS, pagination_options
from ksadk.cli.ui import output_option as cli_output_option


@click.group("agent", context_settings=CONTEXT_SETTINGS)
def agent():
    """Agent 资源管理。"""


@agent.command("list", context_settings=CONTEXT_SETTINGS)
@pagination_options(default_page=1, default_size=20)
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--account-id", envvar="KSYUN_ACCOUNT_ID", help="金山云账号 ID")
@click.option("--framework", help="按框架过滤，支持逗号分隔多个值，如 langgraph,adk")
@dry_run_option()
@cli_output_option()
def list_agents(
    page: int,
    size: int,
    region: str,
    account_id: str,
    framework: str | None,
    dry_run: bool,
    output_mode: str | None,
):
    """列出已部署的 Agent。"""
    _ = output_mode
    run_status_command(
        agent_ref=None,
        agent_option=None,
        show_all=True,
        watch=False,
        interval=2,
        region=region,
        account_id=account_id,
        framework=framework,
        dry_run=dry_run,
        compatibility_alias=False,
        page=page,
        size=size,
    )


@agent.command("status", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@click.option("--agent", "--agent-id", "agent_option", "-a", help="Agent 名称或 ID")
@click.option("--watch", "-w", is_flag=True, help="Watch 模式，持续刷新")
@click.option("--interval", "-i", default=2, help="Watch 刷新间隔 (秒)")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--account-id", envvar="KSYUN_ACCOUNT_ID", help="金山云账号 ID")
@dry_run_option()
@cli_output_option()
def status_agent(
    agent_ref: str | None,
    agent_option: str | None,
    watch: bool,
    interval: int,
    region: str,
    account_id: str,
    dry_run: bool,
    output_mode: str | None,
):
    """查看单个 Agent 状态。"""
    _ = output_mode
    run_status_command(
        agent_ref=agent_ref,
        agent_option=agent_option,
        show_all=False,
        watch=watch,
        interval=interval,
        region=region,
        account_id=account_id,
        dry_run=dry_run,
        compatibility_alias=False,
    )


@agent.command("invoke", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@click.option("--agent", "--agent-id", "agent_option", "-a", help="Agent 名称或 ID")
@click.option("--endpoint", "-e", help="Agent Endpoint URL (覆盖自动获取)")
@click.option("--api-key", help="AgentEngine API Key (覆盖本地配置)")
@click.option(
    "--gateway-token",
    "openclaw_gateway_token",
    envvar="OPENCLAW_GATEWAY_TOKEN",
    help="OpenClaw Gateway token/password（用于 OpenClaw token/password 模式的 /v1/responses）",
)
@click.option("--message", "-m", help="发送的消息 (单次调用模式)")
@click.option("--session", "-s", help="Session ID (可选)")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--local", "-l", is_flag=True, help="连接本地服务 (http://localhost:8080)")
@click.option("--insecure", "-k", is_flag=True, help="跳过 SSL 证书验证 (类似 curl -k)")
@click.option(
    "--transport",
    type=click.Choice(["auto", "chat", "native"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="交互传输层: auto(自动), chat(HTTP /v1/chat/completions), native(Hermes 远端终端)",
)
@click.option(
    "--local-workspace",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="仅 Hermes 远程 native 模式: 先将本地目录同步到远端 workspace",
)
@click.option(
    "--remote-workspace-path",
    help="远端 workspace 子目录 (默认使用本地目录名)",
)
@click.option("--model", help="指定模型名称")
@click.option("--show-thinking", is_flag=True, help="显示模型思考过程")
@cli_output_option()
def invoke_agent(
    agent_ref: str | None,
    agent_option: str | None,
    endpoint: str | None,
    api_key: str | None,
    openclaw_gateway_token: str | None,
    message: str | None,
    session: str | None,
    region: str,
    local: bool,
    insecure: bool,
    transport: str,
    local_workspace: Path | None,
    remote_workspace_path: str | None,
    model: str | None,
    show_thinking: bool,
    output_mode: str | None,
):
    """与 Agent 交互。"""
    _ = output_mode
    ensure_dry_run_supported(
        "agentengine agent invoke",
        suggestion="请使用 `agentengine agent status --output json` 获取结构化元数据。",
    )
    ensure_json_output_supported(
        "agentengine agent invoke",
        suggestion="请使用 `agentengine agent status --output json` 获取结构化元数据。",
    )
    run_invoke_command(
        agent_ref=agent_ref,
        agent_option=agent_option,
        endpoint=endpoint,
        api_key=api_key,
        openclaw_gateway_token=openclaw_gateway_token,
        message=message,
        session=session,
        region=region,
        local=local,
        insecure=insecure,
        transport=transport,
        local_workspace=local_workspace,
        remote_workspace_path=remote_workspace_path,
        model=model,
        show_thinking=show_thinking,
        compatibility_alias=False,
    )


@agent.command("delete", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_refs", nargs=-1)
@click.option("--agent", "--agent-id", "agent_options", "-a", multiple=True, help="Agent 名称或 ID，可重复传入")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="跳过确认")
@click.option("--force", "-f", "assume_yes", is_flag=True, hidden=True, help="(兼容) 跳过确认")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--account-id", envvar="KSYUN_ACCOUNT_ID", help="金山云账号 ID")
@dry_run_option()
@cli_output_option()
def delete_agent(
    agent_refs: tuple[str, ...],
    agent_options: tuple[str, ...],
    assume_yes: bool,
    region: str,
    account_id: str,
    dry_run: bool,
    output_mode: str | None,
):
    """删除一个或多个 Agent。"""
    _ = output_mode
    run_delete_command(
        agent_refs=agent_refs,
        agent_options=agent_options,
        assume_yes=assume_yes,
        region=region,
        account_id=account_id,
        dry_run=dry_run,
        compatibility_alias=False,
    )
