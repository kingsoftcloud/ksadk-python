"""
ksadk web - 启动 Web UI

对于 ADK 项目，使用 ADK Web
对于 LangChain/LangGraph 项目，使用 Chainlit
"""

import click
from pathlib import Path
import os
import subprocess
import sys


@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.argument("agent_dir", default=".", type=click.Path(exists=True))
@click.option("--port", "-p", default=8080, help="Web UI 端口")
@click.option("--model", help="指定模型名称 (覆盖 .env 配置)")
def web(agent_dir: str, port: int, model: str):
    """启动 Web UI

    AGENT_DIR: Agent 项目目录 (默认: 当前目录)
    
    - ADK 项目: 使用 ADK Web UI
    - LangChain/LangGraph 项目: 使用 Chainlit
    """
    from ksadk.detection import FrameworkDetector, FrameworkType

    agent_path = Path(agent_dir).resolve()
    click.echo(f"📁 项目目录: {agent_path}")

    # 设置模型名称 (CLI 参数优先级最高)
    if model:
        os.environ["MODEL_NAME"] = model
        os.environ["OPENAI_MODEL_NAME"] = model
        click.echo(f"🔧 指定模型: {click.style(model, fg='cyan')}")

    # 环境初始化 (加载 .env + 智能默认配置)
    from ksadk.configs import setup_environment
    setup_environment(agent_path)

    # 检测框架
    detector = FrameworkDetector(str(agent_path))
    result = detector.detect()

    if result.type.value == "unknown":
        click.secho("❌ 未检测到支持的框架", fg="red")
        raise SystemExit(1)

    # Map framework types to display names
    framework_map = {
        "adk": "ADK",
        "langchain": "LangChain",
        "langgraph": "LangGraph",
    }
    display_name = framework_map.get(result.type.value, result.name)
    click.echo(f"📦 框架: {click.style(display_name, fg='green')}")

    # 根据框架类型选择 Web UI
    if result.type == FrameworkType.ADK:
        _run_adk_web(agent_path, port)
    else:
        _run_chainlit(result, agent_path, port)


def _run_adk_web(agent_path: Path, port: int):
    """使用 ADK Web UI"""
    click.echo(f"🔧 启动 ADK Web UI")

    cmd = [sys.executable, "-m", "google.adk.cli", "web", ".", "--port", str(port)]

    try:
        subprocess.run(cmd, cwd=str(agent_path), check=True)
    except subprocess.CalledProcessError as e:
        click.secho(f"❌ ADK Web 启动失败: {e}", fg="red")
        raise SystemExit(1)
    except FileNotFoundError:
        click.secho("❌ 未找到 google-adk，请安装: pip install google-adk", fg="red")
        raise SystemExit(1)


def _run_chainlit(result, agent_path: Path, port: int):
    """使用 Chainlit Web UI"""
    # 设置环境变量供 Chainlit 应用使用
    os.environ["KSADK_PROJECT_DIR"] = str(agent_path)
    
    # Chainlit 应用路径
    chainlit_app = Path(__file__).parent.parent / "chainlit" / "app.py"
    
    if not chainlit_app.exists():
        click.secho(f"❌ Chainlit 应用未找到: {chainlit_app}", fg="red")
        raise SystemExit(1)
    
    click.echo(f"\n🚀 启动 Chainlit Web UI...")
    click.echo(f"🌐 Web UI: http://localhost:{port}")
    click.echo(f"🤖 Agent: {result.name}")
    click.echo("\n按 Ctrl+C 停止\n")
    
    cmd = [
        sys.executable, "-m", "chainlit", "run",
        str(chainlit_app),
        "--port", str(port),
        "--host", "0.0.0.0",
    ]
    
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        click.secho(f"❌ Chainlit 启动失败: {e}", fg="red")
        raise SystemExit(1)
    except FileNotFoundError:
        click.secho("❌ 未找到 chainlit，请安装: pip install chainlit", fg="red")
        raise SystemExit(1)
