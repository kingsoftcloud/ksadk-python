"""
ksadk web - 启动 API Server

对于 ADK 项目，直接调用 adk CLI
对于 LangChain/LangGraph 项目，使用自己的 FastAPI 实现
"""

import click
from pathlib import Path
import os
import subprocess
import sys


@click.command(context_settings=dict(help_option_names=['-h', '--help']))
@click.argument('agent_dir', default='.', type=click.Path(exists=True))
@click.option('--port', '-p', default=8080, help='API Server 端口')
def web(agent_dir: str, port: int):
    """启动 API Server (包含 Trace 接口)
    
    AGENT_DIR: Agent 项目目录 (默认: 当前目录)
    """
    from ksadk.detection import FrameworkDetector, FrameworkType
    
    agent_path = Path(agent_dir).resolve()
    click.echo(f"📁 项目目录: {agent_path}")
    
    # 0. 环境初始化 (加载 .env + 智能默认配置)
    from ksadk.configs import setup_environment
    setup_environment(agent_path)
    
    # 1. 检测框架
    detector = FrameworkDetector(str(agent_path))
    result = detector.detect()
    
    if result.type.value == "unknown":
        click.secho("❌ 未检测到支持的框架", fg='red')
        raise SystemExit(1)
    
    # Map framework types to display names
    framework_map = {
        "adk": "ADK",
        "langchain": "LangChain",
        "langgraph": "LangGraph",
        "unknown": "Unknown"
    }
    display_name = framework_map.get(result.type.value, result.name)
    
    click.echo(f"📦 框架: {click.style(display_name, fg='green')}")
    
    # 2. 根据框架类型选择处理方式
    if result.type == FrameworkType.ADK:
        # 直接调用 adk CLI
        _run_adk_cli(agent_path, port, "web")
    else:
        # 使用自己的 FastAPI 实现
        _run_custom_server(result, agent_path, port)


def _run_adk_cli(agent_path: Path, port: int, command: str):
    """直接调用 adk CLI 命令"""
    click.echo(f"🔧 调用 ADK 原生 CLI: adk {command}")
    
    # 使用 subprocess 直接调用 adk 命令
    cmd = [sys.executable, "-m", "google.adk.cli", command, ".", "--port", str(port)]
    
    try:
        subprocess.run(cmd, cwd=str(agent_path), check=True)
    except subprocess.CalledProcessError as e:
        click.secho(f"❌ ADK CLI 执行失败: {e}", fg='red')
        raise SystemExit(1)
    except FileNotFoundError:
        click.secho("❌ 未找到 adk CLI，请确保已安装 google-adk", fg='red')
        raise SystemExit(1)


def _run_custom_server(result, agent_path: Path, port: int):
    """使用自定义 FastAPI Server (用于 LangChain/LangGraph)"""
    from ksadk.runners import create_runner
    from ksadk.server import set_runner
    from ksadk.tracing import setup_tracing
    import uvicorn
    
    # 创建 Runner
    try:
        # 初始化 Tracing
        setup_tracing()
        
        runner = create_runner(result, str(agent_path))
        runner.load_agent()
        click.echo("✅ Agent 加载成功")
    except Exception as e:
        click.secho(f"❌ Agent 加载失败: {e}", fg='red')
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
    
    # 设置 Runner
    set_runner(runner)
    
    click.echo(f"\n🚀 API Server: http://localhost:{port}")
    click.echo(f"📊 Traces API: http://localhost:{port}/debug/trace/session/{{session_id}}")
    click.echo(f"💬 Chat API:   http://localhost:{port}/run_sse")
    click.echo(f"❤️  Health:     http://localhost:{port}/health")
    click.echo("\n按 Ctrl+C 停止\n")
    
    # 直接运行 app 实例（确保当前进程中的路由已注册）
    from ksadk.server import app
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
