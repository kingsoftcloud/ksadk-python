"""
ksadk run - 本地运行 Agent

对于 ADK 项目，直接调用 adk CLI
对于 LangChain/LangGraph/DeepAgents 项目，使用自己的实现
"""

import os
import subprocess
import sys
from pathlib import Path

import click
import uvicorn

from ksadk.cli.error_utils import ensure_json_output_supported, print_exception
from ksadk.cli.local_runtime import reexec_with_project_venv_if_needed
from ksadk.cli.runtime_bootstrap import create_runtime_web_app
from ksadk.cli.ui import (
    print_error,
    print_info,
    print_kv,
    print_success,
    print_title,
    print_warn,
)


@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.argument("agent_dir", default=".", type=click.Path(exists=True))
@click.option("--port", "-p", default=8080, help="Server 端口 (default: 8080)")
@click.option("--interactive", "-i", is_flag=True, help="交互模式 (TUI)")
@click.option("--no-trace", is_flag=True, help="禁用 Tracing")
@click.option("--model", help="指定模型名称 (覆盖 .env 配置)")
@click.option("--show-thinking", is_flag=True, help="显示模型思考过程")
@click.option("--no-stream", is_flag=True, help="禁用流式渲染 (等待完整响应后再渲染)")
@click.option(
    "--no-alt-screen",
    "no_alt_screen",
    is_flag=True,
    help="兼容参数；TUI 已默认使用 inline viewport 并保留终端 scrollback",
)
def run(
    agent_dir: str,
    port: int,
    interactive: bool,
    no_trace: bool,
    model: str,
    show_thinking: bool,
    no_stream: bool,
    no_alt_screen: bool,
):
    """运行 Agent (支持 LangChain / LangGraph / DeepAgents / ADK)

    AGENT_DIR: Agent 项目目录 (默认: 当前目录)
    """
    ensure_json_output_supported(
        "agentengine run",
        suggestion=(
            "请改用 `agentengine agent status --output json` 或 "
            "`agentengine build --output json` 获取结构化信息。"
        ),
    )
    from ksadk.detection import FrameworkDetector

    agent_path = Path(agent_dir).resolve()
    command_args = ["run", str(agent_path), "--port", str(port)]
    if interactive:
        command_args.append("--interactive")
    if no_trace:
        command_args.append("--no-trace")
    if model:
        command_args.extend(["--model", model])
    if show_thinking:
        command_args.append("--show-thinking")
    if no_stream:
        command_args.append("--no-stream")
    reexec_with_project_venv_if_needed(agent_path, command_args)

    print_title("本地运行 Agent")
    print_kv("项目目录", str(agent_path))

    # 设置模型名称 (CLI 参数优先级最高)
    if model:
        os.environ["MODEL_NAME"] = model
        os.environ["OPENAI_MODEL_NAME"] = model
        print_kv("指定模型", model, value_style="#58a6ff")

    # 0. 环境初始化 (加载 .env + 智能默认配置)
    from ksadk.configs import setup_environment

    setup_environment(agent_path)

    # 1. 检测框架类型
    detector = FrameworkDetector(str(agent_path))
    result = detector.detect()

    if result.type.value == "unknown":
        print_error("未检测到支持的框架 (LangChain/LangGraph/DeepAgents/ADK)")
        print_info("提示: 请确保项目包含正确的框架代码")
        raise SystemExit(1)

    framework_map = {
        "adk": "Google ADK",
        "langchain": "LangChain",
        "langgraph": "LangGraph",
        "deepagents": "DeepAgents",
        "unknown": "Unknown",
    }
    framework_name = framework_map.get(result.type.value, result.type.value)

    print_kv("检测到框架", framework_name, value_style="#2da44e")
    print_kv("Agent 名称", str(result.name))
    print_kv("入口点", str(result.entry_point))

    from ksadk.cli.cmd_web import configure_local_runtime_persistence

    configure_local_runtime_persistence(agent_path, result.type.value)

    # 2. 根据框架类型选择处理方式
    # 所有框架统一使用 _run_custom()，让 OTel instrumentation 在同一进程生效。
    _run_custom(
        result, agent_path, port, interactive, no_trace, show_thinking, no_stream, no_alt_screen
    )


def _run_adk_cli(agent_path: Path, port: int = 8080, command: str = "run"):
    """运行 ADK Agent，使用标准 OTLP 配置。"""
    import os

    has_otlp = bool(
        os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or os.getenv("CLOUD_MONITOR_OTLP_ENDPOINT")
        or os.getenv("CLOUD_MONITOR_OTLP_TRACES_ENDPOINT")
    )

    # 必须先初始化 Tracing（在导入 ADK 之前）
    if has_otlp:
        try:
            from ksadk.tracing import setup_tracing

            setup_tracing(
                enable_inmemory=False,
                enable_adk_instrumentation=True,
            )
            print_info("Tracing: Enabled (OTLP + ADK Instrumentation)")
        except Exception as e:
            print_warn(f"OTLP tracing 初始化失败: {e}")

    print_kv("调用 ADK 原生 CLI", f"adk {command}")

    # 子进程继承标准 OTLP 环境变量；项目内 instrumentation 负责创建 spans。
    if command == "run":
        cmd = [sys.executable, "-m", "google.adk.cli", "run", "."]
    else:
        cmd = [sys.executable, "-m", "google.adk.cli", "web", ".", "--port", str(port)]

    env = os.environ.copy()

    try:
        subprocess.run(cmd, cwd=str(agent_path), check=True, env=env)
    except subprocess.CalledProcessError as e:
        print_exception("ADK CLI 执行失败", e)
        raise SystemExit(1)
    except FileNotFoundError:
        print_error("未找到 adk CLI，请确保已安装 google-adk")
        raise SystemExit(1)


def _run_custom(
    result,
    agent_path: Path,
    port: int,
    interactive: bool,
    no_trace: bool,
    show_thinking: bool,
    no_stream: bool = False,
    no_alt_screen: bool = False,
):
    """Run one detected project through the canonical RuntimeAdapter composition."""

    # 初始化 Tracing
    if not no_trace:
        try:
            import os

            from ksadk.tracing import setup_tracing

            has_otlp = bool(
                os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
                or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
            )

            setup_tracing(
                enable_inmemory=True,
                enable_langfuse=None,
            )

            if has_otlp:
                print_info("Tracing: Enabled (InMemory + OTLP HTTP)")
            else:
                print_info("Tracing: Enabled")
        except Exception as e:
            print_warn(f"Tracing 初始化失败: {e}")

    if interactive:
        del show_thinking, no_stream, no_alt_screen
        print_error(
            "RuntimeAdapter TUI 尚未接通；请使用 `ksadk web` 或不带 --interactive 启动"
        )
        raise SystemExit(2)

    try:
        runtime_app = create_runtime_web_app(result, agent_path)
    except Exception as e:
        print_exception("RuntimeAdapter 初始化失败", e)
        raise SystemExit(1)

    print_success(f"Server running at http://127.0.0.1:{port}")
    print_kv("API Docs", f"http://127.0.0.1:{port}/docs")
    print_kv("Chat API", f"http://127.0.0.1:{port}/chat")
    print_info("Press Ctrl+C to stop")
    uvicorn.run(runtime_app, host="127.0.0.1", port=port)
