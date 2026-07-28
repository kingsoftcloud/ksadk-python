# -*- coding: utf-8 -*-
"""ksadk a2a 子命令 — A2A 协议服务与本地试调 (goal-16,a2a-center-productization-2026-07 清洁重做)。

基于 goal-05 清洁重写的 A2A API(``A2AProtocolServer`` / ``build_agent_card`` /
``add_a2a_protocol_routes`` / ``A2ASpaceClient``),不使用旧 0.3.x demo API
(``AgentCardBuilder`` / ``KsA2AServer``,已删)——那是 Windows ``No such command 'a2a'``
的根因(import 失败被 ``except ImportError: pass`` 静默吞)。

子命令:serve / card / register / discover / call / status / cancel。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Sequence

import click
import uvicorn
from a2a.types import TaskState
from google.protobuf.json_format import MessageToJson

import ksadk.configs as configs
from ksadk.a2a import (
    A2AConfig,
    A2APlatformTask,
    A2ASpaceClient,
    add_a2a_protocol_routes,
    build_agent_card,
)
from ksadk.cli.local_runtime import reexec_with_project_venv_if_needed
from ksadk.cli.resource_common import CONTEXT_SETTINGS
from ksadk.detection import FrameworkDetector
from ksadk.runners.factory import create_runner
from ksadk.runtime.adapter import RuntimeAdapter
from ksadk.runtime.framework_adapters import ADKRuntimeAdapter, LangGraphRuntimeAdapter
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter

_HELP = dict(help_option_names=["-h", "--help"])
_DEFAULT_TASK_STORE_DSN = "sqlite+aiosqlite:///.agentengine/a2a_tasks.db"


def _resolve_task_store_dsn(task_store_dsn: str, agent_path: Path) -> str:
    """相对路径的 sqlite DSN 锚定到 agent_path(而非 cwd),并确保父目录存在。

    绝对 DSN(postgresql:// 等)原样返回。
    """
    prefix = "sqlite+aiosqlite:///"
    if not task_store_dsn.startswith(prefix):
        return task_store_dsn
    raw_path = task_store_dsn[len(prefix) :]
    if raw_path.startswith("/") or not raw_path:
        return task_store_dsn
    resolved = (agent_path / raw_path).resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return f"{prefix}{resolved}"


@click.group("a2a", context_settings=CONTEXT_SETTINGS, help="A2A 协议服务与本地试调")
def a2a():
    """A2A 协议命令组(serve / card / register / discover / call / status / cancel)。"""


# ---------------------------------------------------------------------------
# serve / card(本地 A2A server 与 AgentCard)
# ---------------------------------------------------------------------------


@a2a.command("serve", context_settings=_HELP)
@click.argument(
    "agent_path", type=click.Path(exists=True, file_okay=False, path_type=Path), default="."
)
@click.option("--host", default="127.0.0.1", show_default=True, help="服务监听地址")
@click.option("--port", default=8081, show_default=True, type=int, help="服务端口")
@click.option("--url", default=None, help="Agent Card 对外宣告地址(默认 http://host:port)")
@click.option("--name", default=None, help="覆盖 Agent 名称")
@click.option("--description", default="", help="覆盖 Agent 描述")
@click.option("--skill", "skills", multiple=True, help="可重复传入,追加 Agent Card 技能")
@click.option(
    "--task-store-dsn",
    default=_DEFAULT_TASK_STORE_DSN,
    show_default=True,
    help="durable task store DSN(生产用 postgresql+asyncpg://)",
)
@click.option(
    "--include-reasoning/--no-include-reasoning",
    default=True,
    show_default=True,
    help="将可展示的 reasoning 作为 adk_thought artifact 流式输出",
)
@click.option("--no-trace", is_flag=True, help="禁用 Tracing")
def serve(
    agent_path: Path,
    host: str,
    port: int,
    url: str | None,
    name: str | None,
    description: str,
    skills: Sequence[str],
    task_store_dsn: str,
    include_reasoning: bool,
    no_trace: bool,
):
    """把本地 agent 暴露为 A2A 协议服务(JSONRPC + REST + AgentCard)。"""
    agent_path = agent_path.resolve()
    command_args = ["a2a", "serve", str(agent_path), "--host", host, "--port", str(port)]
    if url:
        command_args.extend(["--url", url])
    if name:
        command_args.extend(["--name", name])
    if description:
        command_args.extend(["--description", description])
    for skill in skills:
        command_args.extend(["--skill", skill])
    if task_store_dsn != _DEFAULT_TASK_STORE_DSN:
        command_args.extend(["--task-store-dsn", task_store_dsn])
    if not include_reasoning:
        command_args.append("--no-include-reasoning")
    if no_trace:
        command_args.append("--no-trace")
    reexec_with_project_venv_if_needed(agent_path, command_args)
    task_store_dsn = _resolve_task_store_dsn(task_store_dsn, agent_path)
    detection_result, runner = _load_runner(agent_path, no_trace=no_trace)
    from fastapi import FastAPI

    app = FastAPI(title=f"ksadk A2A: {detection_result.name}")
    config = A2AConfig(
        enabled=True,
        base_url=url or f"http://{host}:{port}",
        agent_name=name or detection_result.name,
        description=description,
        skills=list(skills),
        task_store_dsn=task_store_dsn,
        include_reasoning=include_reasoning,
    )
    runtime_type = detection_result.type.value
    runtime_adapter = _select_runtime_adapter(runtime_type, runner)
    from ksadk.a2a.task_adapter import A2ARuntimeTaskAdapter

    add_a2a_protocol_routes(
        app,
        runner,
        config,
        task_adapter=A2ARuntimeTaskAdapter(runtime_adapter, runtime_type=runtime_type),
    )
    click.echo(f"A2A agent card: {(url or f'http://{host}:{port}')}/.well-known/agent-card.json")
    uvicorn.run(app, host=host, port=port)


@a2a.command("card", context_settings=_HELP)
@click.argument(
    "agent_path", type=click.Path(exists=True, file_okay=False, path_type=Path), default="."
)
@click.option(
    "--url",
    default="http://127.0.0.1:8081",
    show_default=True,
    help="Agent Card 对外宣告地址",
)
@click.option("--name", default=None, help="覆盖 Agent 名称")
@click.option("--description", default="", help="覆盖 Agent 描述")
@click.option("--skill", "skills", multiple=True, help="可重复传入,追加 Agent Card 技能")
def card(agent_path: Path, url: str, name: str | None, description: str, skills: Sequence[str]):
    """打印该 agent 的 wire 1.0 AgentCard(supportedInterfaces)。"""
    detection_result = _detect_project(agent_path)
    card_obj = build_agent_card(
        name=name or detection_result.name,
        base_url=url,
        description=description,
        skills=list(skills),
    )
    click.echo(MessageToJson(card_obj, indent=2))


@a2a.command("register", context_settings=_HELP)
@click.argument(
    "agent_path", type=click.Path(exists=True, file_okay=False, path_type=Path), default="."
)
@click.option("--url", required=True, help="Agent Card 对外宣告地址")
@click.option("--name", default=None, help="覆盖 Agent 名称")
@click.option("--description", default="", help="覆盖 Agent 描述")
@click.option("--skill", "skills", multiple=True, help="可重复传入,追加 Agent Card 技能")
def register(agent_path: Path, url: str, name: str | None, description: str, skills: Sequence[str]):
    """构造并打印用于 Space 注册的 AgentCard(本地试调;server 侧 KOP 注册见产品化文档)。"""
    detection_result = _detect_project(agent_path)
    card_obj = build_agent_card(
        name=name or detection_result.name,
        base_url=url,
        description=description,
        skills=list(skills),
    )
    click.echo(
        "# 本地 AgentCard 预览；平台注册使用 GetA2AAgentCard + CreateA2AAgent，"
        "hosted 注册不接收用户手写 Card:"
    )
    click.echo(MessageToJson(card_obj, indent=2))


# ---------------------------------------------------------------------------
# discover / call / status / cancel(Space 内动态发现与调用,经 A2ASpaceClient)
# ---------------------------------------------------------------------------


def _space_client(space_id: str | None = None) -> A2ASpaceClient:
    """从环境构造 A2ASpaceClient;Space 未配置时显式报错(不静默)。"""
    try:
        return A2ASpaceClient.from_env(space_id=space_id)
    except ValueError as exc:
        raise click.ClickException(
            f"A2A Space 未配置:{exc}。"
            "请传 --space-id，或由 AgentEngine 部署注入 KSADK_A2A_SPACE_IDS；同时需要"
            "KSADK_A2A_CONTROL_PLANE_URL 和 audience workload token。"
        ) from exc


@a2a.command("discover", context_settings=_HELP)
@click.option("--space-id", default=None, help="本次发现使用的 A2A Space ID")
@click.option("--prompt", default=None, help="发现关键词(受控匹配)")
@click.option("--skill", default=None, help="按技能过滤")
def discover(space_id: str | None, prompt: str | None, skill: str | None):
    """发现 Space 中 hosted/external Agent 的 latest AgentCard。"""

    async def _run() -> None:
        client = _space_client(space_id)
        agents = await client.discover(prompt=prompt, skill=skill)
        for agent in agents:
            click.echo(
                json.dumps(
                    {
                        "agent_id": agent.agent_id,
                        "version_id": agent.version_id,
                        "source": agent.source,
                        "name": getattr(agent.agent_card, "name", ""),
                        "route_kind": agent.route_kind,
                        "callable": agent.callable,
                        "blocked_reason": agent.blocked_reason,
                    },
                    ensure_ascii=False,
                )
            )

    asyncio.run(_run())


@a2a.command("call", context_settings=_HELP)
@click.option("--space-id", default=None, help="本次调用使用的 A2A Space ID")
@click.argument("agent_id")
@click.argument("message")
def call(space_id: str | None, agent_id: str, message: str):
    """向 Space 中某 Agent 发送消息,返回首个 Task。"""

    async def _run() -> None:
        client = _space_client(space_id)
        task = await client.send_message(agent_id, message)
        click.echo(json.dumps(_platform_task_summary(task), ensure_ascii=False))

    asyncio.run(_run())


@a2a.command("status", context_settings=_HELP)
@click.option("--space-id", default=None, help="Task 创建时使用的 A2A Space ID")
@click.argument("task_id")
def status(space_id: str | None, task_id: str):
    """查询某 A2A Task 的状态。"""

    async def _run() -> None:
        client = _space_client(space_id)
        task = await client.get_task(task_id)
        click.echo(json.dumps(_platform_task_summary(task), ensure_ascii=False))

    asyncio.run(_run())


@a2a.command("cancel", context_settings=_HELP)
@click.option("--space-id", default=None, help="Task 创建时使用的 A2A Space ID")
@click.argument("task_id")
def cancel(space_id: str | None, task_id: str):
    """取消某 A2A Task。"""

    async def _run() -> None:
        client = _space_client(space_id)
        task = await client.cancel(task_id)
        click.echo(json.dumps(_platform_task_summary(task), ensure_ascii=False))

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 辅助(与旧版一致,框架无关)
# ---------------------------------------------------------------------------


def _platform_task_summary(task: A2APlatformTask) -> dict[str, str | None]:
    remote_task = task.remote_task
    remote_status = getattr(remote_task, "status", None)
    return {
        "task_id": task.id,
        "status": TaskState.Name(remote_status.state) if remote_status is not None else None,
    }


def _detect_project(agent_path: Path):
    configs.setup_environment(agent_path)
    result = FrameworkDetector(str(agent_path)).detect()
    if result.type.value == "unknown":
        raise click.ClickException("未检测到支持的框架 (LangChain/LangGraph/DeepAgents/ADK)")
    return result


def _load_runner(agent_path: Path, *, no_trace: bool):
    detection_result = _detect_project(agent_path)
    if not no_trace:
        _setup_tracing(detection_result.type.value)
    runner = create_runner(detection_result, str(agent_path))
    runner.load_agent()
    return detection_result, runner


def _select_runtime_adapter(runtime_type: str, runner) -> RuntimeAdapter:
    # langchain create_agent 产物是 LangGraph CompiledStateGraph，
    # 与 langgraph 共用 time-travel resume。
    if runtime_type in ("langgraph", "langchain"):
        return LangGraphRuntimeAdapter(runner)
    if runtime_type == "adk":
        return ADKRuntimeAdapter(runner)
    return RunnerRuntimeAdapter(runner, runtime_type=runtime_type)


def _setup_tracing(framework_type: str) -> None:
    try:
        import os

        from ksadk.tracing import setup_tracing

        use_callback_only = os.getenv("LANGFUSE_USE_CALLBACK", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        setup_tracing(
            enable_inmemory=True,
            use_callback_only=use_callback_only,
        )
    except Exception:
        return
