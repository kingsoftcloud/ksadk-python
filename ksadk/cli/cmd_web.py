"""
ksadk web - 启动 Web UI

用于本地调试 Agent 的 Invoke UI（非云端 Dashboard）。
- ADK 项目: 使用 ADK Web
- LangChain/LangGraph/DeepAgents 项目: 使用 Chainlit
"""

import click
from pathlib import Path
import os
import subprocess
import sys
from ksadk.cli.error_utils import ensure_json_output_supported, print_exception
from ksadk.cli.ui import (
    print_error,
    print_info,
    print_kv,
    print_success,
    print_title,
)


@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.argument("agent_dir", default=".", type=click.Path(exists=True))
@click.option("--port", "-p", default=8080, help="Web UI 端口")
@click.option("--model", help="指定模型名称 (覆盖 .env 配置)")
def web(agent_dir: str, port: int, model: str):
    """启动本地调试 Web UI（Invoke UI）

    \b
    AGENT_DIR: Agent 项目目录 (默认: 当前目录)

    \b
    用途说明:
      本地调试 Agent Invoke UI（非云端 Dashboard）
      ADK 项目使用 ADK Web UI
      LangChain/LangGraph/DeepAgents 项目使用 Chainlit
    """
    ensure_json_output_supported(
        "agentengine web",
        suggestion="请改用 `agentengine dashboard open` 或 `agentengine agent status --output json`。",
    )
    from ksadk.detection import FrameworkDetector, FrameworkType

    agent_path = Path(agent_dir).resolve()
    print_title("启动本地调试 Web UI")
    print_kv("项目目录", str(agent_path))

    # 设置模型名称 (CLI 参数优先级最高)
    if model:
        os.environ["MODEL_NAME"] = model
        os.environ["OPENAI_MODEL_NAME"] = model
        print_kv("指定模型", model, value_style="#58a6ff")

    # 环境初始化 (加载 .env + 智能默认配置)
    from ksadk.configs import setup_environment
    setup_environment(agent_path)

    # 检测框架
    detector = FrameworkDetector(str(agent_path))
    result = detector.detect()

    if result.type.value == "unknown":
        print_error("未检测到支持的框架")
        raise SystemExit(1)

    # Map framework types to display names
    framework_map = {
        "adk": "ADK",
        "langchain": "LangChain",
        "langgraph": "LangGraph",
        "deepagents": "DeepAgents",
    }
    display_name = framework_map.get(result.type.value, result.name)
    print_kv("框架", display_name, value_style="#2da44e")

    # 根据框架类型选择 Web UI
    if result.type == FrameworkType.ADK:
        _run_adk_web(agent_path, port)
    else:
        _run_chainlit(result, agent_path, port)


def _run_adk_web(agent_path: Path, port: int):
    """使用 ADK Web UI"""
    print_info("启动 ADK Web UI")

    cmd = [sys.executable, "-m", "google.adk.cli", "web", ".", "--port", str(port)]

    try:
        subprocess.run(cmd, cwd=str(agent_path), check=True)
    except subprocess.CalledProcessError as e:
        print_exception("ADK Web 启动失败", e)
        raise SystemExit(1)
    except FileNotFoundError:
        print_error("未找到 google-adk，请安装: pip install google-adk")
        raise SystemExit(1)


def _run_chainlit(result, agent_path: Path, port: int):
    """使用 Chainlit Web UI"""
    # 设置环境变量供 Chainlit 应用使用
    os.environ["KSADK_PROJECT_DIR"] = str(agent_path)
    
    # Chainlit 应用路径
    chainlit_app = Path(__file__).parent.parent / "chainlit" / "app.py"
    
    if not chainlit_app.exists():
        print_error(f"Chainlit 应用未找到: {chainlit_app}")
        raise SystemExit(1)
    
    print_success("启动 Chainlit Web UI")
    print_kv("Web UI", f"http://localhost:{port}", value_style="#58a6ff")
    print_kv("Agent", result.name)
    print_info("按 Ctrl+C 停止")
    
    cmd = [
        sys.executable, "-m", "chainlit", "run",
        str(chainlit_app),
        "--port", str(port),
        "--host", "0.0.0.0",
    ]
    
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print_exception("Chainlit 启动失败", e)
        raise SystemExit(1)
    except FileNotFoundError:
        print_error("未找到 chainlit，请安装: pip install chainlit")
        raise SystemExit(1)
