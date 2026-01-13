"""
ksadk run - 本地运行 Agent

对于 ADK 项目，直接调用 adk CLI
对于 LangChain/LangGraph 项目，使用自己的实现
"""

import click
import asyncio
import subprocess
import sys
import os
from pathlib import Path


@click.command(context_settings=dict(help_option_names=['-h', '--help']))
@click.argument('agent_dir', default='.', type=click.Path(exists=True))
@click.option('--port', '-p', default=8080, help='Server 端口 (default: 8080)')
@click.option('--interactive', '-i', is_flag=True, help='交互模式')
@click.option('--no-trace', is_flag=True, help='禁用 Tracing')
def run(agent_dir: str, port: int, interactive: bool, no_trace: bool):
    """运行 Agent (支持 LangChain / LangGraph / ADK)
    
    AGENT_DIR: Agent 项目目录 (默认: 当前目录)
    """
    from ksadk.detection import FrameworkDetector, FrameworkType
    
    agent_path = Path(agent_dir).resolve()
    click.echo(f"📁 项目目录: {agent_path}")
    
    # 0. 环境初始化 (加载 .env + 智能默认配置)
    from ksadk.configs import setup_environment
    setup_environment(agent_path)
    
    # 1. 检测框架类型
    detector = FrameworkDetector(str(agent_path))
    result = detector.detect()
    
    if result.type.value == "unknown":
        click.secho("❌ 未检测到支持的框架 (LangChain/LangGraph/ADK)", fg='red')
        click.echo("提示: 请确保项目包含正确的框架代码")
        raise SystemExit(1)
    
    click.echo(f"📦 检测到框架: {click.style(result.name, fg='green')}")
    click.echo(f"🎯 入口点: {result.entry_point}")
    
    # 2. 根据框架类型选择处理方式
    # 所有框架统一使用 _run_custom() 以支持 Langfuse 自动插桩
    # (Langfuse instrumentation 需要在同一进程内生效)
    _run_custom(result, agent_path, port, interactive, no_trace)


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
            click.echo("📊 Tracing: \033[92mEnabled\033[0m (Langfuse + ADK Instrumentation)")
        except Exception as e:
            click.secho(f"⚠️ Langfuse 初始化失败: {e}", fg='yellow')
    
    click.echo(f"🔧 调用 ADK 原生 CLI: adk {command}")
    
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
        click.echo("")
        click.echo("💡 ADK 项目 Langfuse 集成提示:")
        click.echo("   在 agent.py 中添加以下代码:")
        click.echo("   from openinference.instrumentation.google_adk import GoogleADKInstrumentor")
        click.echo("   GoogleADKInstrumentor().instrument()")
        click.echo("")
    
    try:
        subprocess.run(cmd, cwd=str(agent_path), check=True, env=env)
    except subprocess.CalledProcessError as e:
        click.secho(f"❌ ADK CLI 执行失败: {e}", fg='red')
        raise SystemExit(1)
    except FileNotFoundError:
        click.secho("❌ 未找到 adk CLI，请确保已安装 google-adk", fg='red')
        raise SystemExit(1)


def _run_custom(result, agent_path: Path, port: int, interactive: bool, no_trace: bool):
    """使用自定义实现 (LangChain/LangGraph)"""
    from ksadk.runners.unified_runner import UnifiedRunner
    
    # 初始化 Tracing
    if not no_trace:
        try:
            from ksadk.tracing import setup_tracing
            import os
            
            # Auto-detect Langfuse from environment
            has_langfuse = bool(os.getenv("LANGFUSE_PUBLIC_KEY"))
            
            setup_tracing(
                enable_inmemory=True,
                enable_langfuse=has_langfuse,
            )
            
            if has_langfuse:
                click.echo("📊 Tracing: \033[92mEnabled\033[0m (InMemory + Langfuse)")
            else:
                click.echo("📊 Tracing: \033[92mEnabled\033[0m")
        except Exception as e:
            click.secho(f"⚠️ Tracing 初始化失败: {e}", fg='yellow')
    
    # 创建 Runner
    try:
        click.echo("🔄 初始化 Runner...", nl=False)
        runner = UnifiedRunner.create(result, str(agent_path))
        runner.load_agent()
        click.echo("\r✅ Agent 加载成功        ")
    except Exception as e:
        click.echo("\r❌ Agent 加载失败        ")
        click.secho(f"Error: {e}", fg='red')
        # import traceback
        # traceback.print_exc()
        raise SystemExit(1)
    
    # 运行
    if interactive:
        click.clear()
        click.secho("🤖 KsADK Interactive Mode", fg='blue', bold=True)
        click.echo(f"Framework: {result.name}")
        click.echo("Type 'exit' to quit.\n")
        asyncio.run(UnifiedRunner.run_interactive(runner))
    else:
        click.secho(f"\n🚀 Server running at http://0.0.0.0:{port}", fg='green', bold=True)
        click.echo("   - API Docs: http://0.0.0.0:{}/docs".format(port))
        click.echo("   - Chat API: http://0.0.0.0:{}/chat".format(port))
        click.echo("\nPress Ctrl+C to stop")
        runner.run_server(port=port)
