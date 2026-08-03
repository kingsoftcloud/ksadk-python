"""agentengine studio - start the local-first Agent authoring control plane."""

from __future__ import annotations

import secrets
import webbrowser
from pathlib import Path

import click
import uvicorn

from ksadk.cli.ui import print_info, print_kv, print_success, print_title
from ksadk.studio.api import create_studio_app


@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.argument("workspace", default=".", type=click.Path())
@click.option("--port", "-p", default=7831, type=click.IntRange(1, 65535))
@click.option("--no-open", is_flag=True, help="仅打印 URL，不自动打开浏览器")
def studio(workspace: str, port: int, no_open: bool) -> None:
    """启动本地 AgentKit Studio。

    \b
    WORKSPACE: Agent 工作区目录，目录不存在时自动初始化。
    """

    root = Path(workspace).expanduser().resolve()
    session_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(24)
    app = create_studio_app(
        root,
        session_token=session_token,
        csrf_token=csrf_token,
    )
    base_url = f"http://127.0.0.1:{port}/"
    launch_url = f"{base_url}#session={session_token}"

    print_title("启动 AgentKit Local Studio")
    print_kv("工作区", str(root))
    print_kv("访问地址", launch_url, value_style="#58a6ff")
    print_success("构建与运行均在本地执行")
    print_info("按 Ctrl+C 停止")
    if not no_open:
        webbrowser.open(launch_url)
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        access_log=False,
    )
