"""
ksadk run - 本地运行 Agent

对于 ADK 项目，直接调用 adk CLI
对于 LangChain/LangGraph/DeepAgents 项目，使用自己的实现
"""

import click
import asyncio
import subprocess
import sys
import os
from pathlib import Path
from ksadk.cli.error_utils import ensure_json_output_supported, print_exception
from ksadk.cli.local_runtime import reexec_with_project_venv_if_needed
from ksadk.cli.ui import (
    print_error,
    print_info,
    print_kv,
    print_rule,
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
def run(agent_dir: str, port: int, interactive: bool, no_trace: bool, model: str, show_thinking: bool, no_stream: bool):
    """运行 Agent (支持 LangChain / LangGraph / DeepAgents / ADK)

    AGENT_DIR: Agent 项目目录 (默认: 当前目录)
    """
    ensure_json_output_supported(
        "agentengine run",
        suggestion="请改用 `agentengine agent status --output json` 或 `agentengine build --output json` 获取结构化信息。",
    )
    from ksadk.detection import FrameworkDetector, FrameworkType

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
        "unknown": "Unknown"
    }
    framework_name = framework_map.get(result.type.value, result.type.value)

    print_kv("检测到框架", framework_name, value_style="#2da44e")
    print_kv("Agent 名称", str(result.name))
    print_kv("入口点", str(result.entry_point))

    # 2. 根据框架类型选择处理方式
    # 所有框架统一使用 _run_custom() 以支持 Langfuse 自动插桩
    # (Langfuse instrumentation 需要在同一进程内生效)
    _run_custom(result, agent_path, port, interactive, no_trace, show_thinking, no_stream)


def _run_adk_cli(agent_path: Path, port: int = 8080, command: str = "run"):
    """运行 ADK Agent，支持 Langfuse tracing

    对于 Langfuse 集成，我们需要在同一进程内运行 ADK，
    因为 OpenTelemetry instrumentation 只在进程内生效。
    """
    import os

    has_langfuse = bool(os.getenv("LANGFUSE_PUBLIC_KEY"))

    # 必须先初始化 Tracing（在导入 ADK 之前）
    if has_langfuse:
        try:
            from ksadk.tracing import setup_tracing

            setup_tracing(
                enable_inmemory=False,
                enable_langfuse=True,
                enable_adk_instrumentation=True,
            )
            print_info("Tracing: Enabled (Langfuse + ADK Instrumentation)")
        except Exception as e:
            print_warn(f"Langfuse 初始化失败: {e}")

    print_kv("调用 ADK 原生 CLI", f"adk {command}")

    # 使用 subprocess（Langfuse instrumentation 在子进程中不生效，但环境变量会传递）
    # ADK CLI 本身不支持 Langfuse，需要用户在项目中集成
    if command == "run":
        cmd = [sys.executable, "-m", "google.adk.cli", "run", "."]
    else:
        cmd = [sys.executable, "-m", "google.adk.cli", "web", ".", "--port", str(port)]

    # 传递 Langfuse 环境变量
    env = os.environ.copy()
    langfuse_vars = ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"]
    for var in langfuse_vars:
        if var in os.environ:
            env[var] = os.environ[var]

    # 提示用户如何在 ADK 项目中启用 Langfuse
    if has_langfuse:
        print_rule("ADK 项目 Langfuse 集成提示")
        print_info("在 agent.py 中添加以下代码:")
        print_info("from openinference.instrumentation.google_adk import GoogleADKInstrumentor")
        print_info("GoogleADKInstrumentor().instrument()")

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
):
    """使用自定义实现 (LangChain/LangGraph/DeepAgents)"""
    from ksadk.runners.factory import create_runner

    # 初始化 Tracing
    if not no_trace:
        try:
            from ksadk.tracing import setup_tracing
            import os

            # Auto-detect Langfuse from environment
            has_langfuse = bool(os.getenv("LANGFUSE_PUBLIC_KEY"))

            use_callback_only = os.getenv("LANGFUSE_USE_CALLBACK", "").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )

            setup_tracing(
                enable_inmemory=True,
                enable_langfuse=has_langfuse,
                use_callback_only=use_callback_only,
            )

            if has_langfuse:
                print_info(f"Tracing: Enabled (InMemory + Langfuse, CallbackOnly={use_callback_only})")
            else:
                print_info("Tracing: Enabled")
        except Exception as e:
            print_warn(f"Tracing 初始化失败: {e}")

    # 创建 Runner
    try:
        print_info("初始化 Runner...")
        runner = create_runner(result, str(agent_path))
        runner.load_agent()
        print_success("Agent 加载成功")
    except Exception as e:
        print_exception("Agent 加载失败", e)
        # import traceback
        # traceback.print_exc()
        raise SystemExit(1)

    # 运行
    if interactive:
        # TUI 交互模式
        from ksadk.tui import AgentTUI
        app = AgentTUI(
            runner=runner,
            show_thinking=show_thinking,
            project_dir=str(agent_path),
        )
        app.run()
    else:
        print_success(f"Server running at http://0.0.0.0:{port}")
        print_kv("API Docs", f"http://0.0.0.0:{port}/docs")
        print_kv("Chat API", f"http://0.0.0.0:{port}/chat")
        print_info("Press Ctrl+C to stop")
        runner.run_server(port=port)
