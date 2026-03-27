from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

import click
import uvicorn

import ksadk.configs as configs
from ksadk.a2a import AgentCardBuilder, KsA2AServer
from ksadk.cli.resource_common import CONTEXT_SETTINGS
from ksadk.detection import FrameworkDetector
from ksadk.runners.unified_runner import UnifiedRunner


@click.group("a2a", context_settings=CONTEXT_SETTINGS, help="A2A 协议服务与 Agent Card")
def a2a():
    """A2A protocol helpers."""


@a2a.command("serve", context_settings=dict(help_option_names=["-h", "--help"]))
@click.argument(
    "agent_dir",
    default=".",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option("--host", default="0.0.0.0", show_default=True, help="服务监听地址")
@click.option("--port", default=8081, show_default=True, type=int, help="服务端口")
@click.option("--url", default=None, help="Agent Card 对外宣告地址")
@click.option("--name", default=None, help="覆盖 Agent 名称")
@click.option("--description", default="", help="覆盖 Agent 描述")
@click.option("--skill", "skills", multiple=True, help="可重复传入，追加 Agent Card 技能")
@click.option("--no-trace", is_flag=True, help="禁用 Tracing")
def serve(
    agent_dir: Path,
    host: str,
    port: int,
    url: str | None,
    name: str | None,
    description: str,
    skills: Sequence[str],
    no_trace: bool,
):
    """启动 A2A 协议服务。"""
    agent_path = agent_dir.resolve()
    detection_result, runner = _load_runner(agent_path, no_trace=no_trace)
    app_name = name or detection_result.name
    public_url = url or f"http://127.0.0.1:{port}"
    server = KsA2AServer(
        runner=runner,
        app_name=app_name,
        url=public_url,
        description=description,
        skills=skills,
    )
    uvicorn.run(server.build(), host=host, port=port)


@a2a.command("card", context_settings=dict(help_option_names=["-h", "--help"]))
@click.argument(
    "agent_dir",
    default=".",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--url",
    default="http://127.0.0.1:8081",
    show_default=True,
    help="Agent Card 对外宣告地址",
)
@click.option("--name", default=None, help="覆盖 Agent 名称")
@click.option("--description", default="", help="覆盖 Agent 描述")
@click.option("--skill", "skills", multiple=True, help="可重复传入，追加 Agent Card 技能")
def card(
    agent_dir: Path,
    url: str,
    name: str | None,
    description: str,
    skills: Sequence[str],
):
    """输出 Agent Card JSON。"""
    agent_path = agent_dir.resolve()
    detection_result = _detect_project(agent_path)
    payload = _build_agent_card_payload(
        app_name=name or detection_result.name,
        url=url,
        description=description,
        skills=skills,
    )
    click.echo(json.dumps(payload, ensure_ascii=False))


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
    runner = UnifiedRunner.create(detection_result, str(agent_path))
    runner.load_agent()
    return detection_result, runner


def _setup_tracing(framework_type: str) -> None:
    try:
        import os

        from ksadk.tracing import setup_tracing

        has_langfuse = bool(os.getenv("LANGFUSE_PUBLIC_KEY"))
        is_langchain = framework_type in ("langchain", "langgraph", "deepagents")
        setup_tracing(
            enable_inmemory=True,
            enable_langfuse=has_langfuse,
            use_callback_only=is_langchain,
        )
    except Exception:
        return


def _build_agent_card_payload(
    *,
    app_name: str,
    url: str,
    description: str,
    skills: Sequence[str],
) -> dict:
    card = AgentCardBuilder(
        name=app_name,
        url=url,
        description=description,
        skills=skills,
    ).build()
    return card.model_dump(mode="json")
