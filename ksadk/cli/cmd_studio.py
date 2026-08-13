"""agentengine studio - start the local-first Agent authoring control plane."""

from __future__ import annotations

import os
import secrets
import webbrowser
from pathlib import Path

import click
import uvicorn

from ksadk.cli.env_options import load_env_file
from ksadk.cli.ui import print_info, print_kv, print_success, print_title
from ksadk.studio.api import create_studio_app
from ksadk.studio.service import StudioService

_MODEL_ENV_KEYS = (
    "OPENAI_API_BASE",
    "OPENAI_API_KEY",
    "OPENAI_MODEL_NAME",
)


@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.argument("workspace", default=".", type=click.Path())
@click.option("--port", "-p", default=8080, type=click.IntRange(1, 65535))
@click.option("--no-open", is_flag=True, help="仅打印 URL，不自动打开浏览器")
@click.option(
    "--env-file",
    type=click.Path(exists=True, dir_okay=False),
    help="模型环境文件；只读取 OPENAI_API_BASE/API_KEY/MODEL_NAME",
)
@click.option(
    "--codex-proxy",
    type=click.Choice(["inherit", "auto", "forced", "direct"]),
    default="inherit",
    show_default=True,
    help="Codex Responses→Chat 代理策略",
)
def studio(
    workspace: str,
    port: int,
    no_open: bool,
    env_file: str | None,
    codex_proxy: str,
) -> None:
    """启动本地 AgentKit Studio。

    \b
    WORKSPACE: Agent 工作区目录，目录不存在时自动初始化。
    """

    root = Path(workspace).expanduser().resolve()
    managed_keys = (*_MODEL_ENV_KEYS, "KSADK_CODEX_USE_PROXY")
    previous = {key: os.environ.get(key) for key in managed_keys}
    previously_present = {key for key in managed_keys if key in os.environ}
    try:
        if env_file:
            try:
                values = load_env_file(env_file)
            except ValueError as exc:
                raise click.ClickException(str(exc)) from exc
            loaded = 0
            for key, value in values.items():
                if key not in _MODEL_ENV_KEYS or not value:
                    continue
                loaded += 1
                if key not in os.environ:
                    os.environ[key] = value
            print_kv("模型环境", f"已安全加载 {loaded}/{len(_MODEL_ENV_KEYS)} 个字段")
        if codex_proxy == "forced":
            os.environ["KSADK_CODEX_USE_PROXY"] = "1"
        elif codex_proxy == "direct":
            os.environ["KSADK_CODEX_USE_PROXY"] = "0"
        elif codex_proxy == "auto":
            os.environ.pop("KSADK_CODEX_USE_PROXY", None)
        session_token = os.environ.get("KSADK_STUDIO_SESSION_TOKEN") or secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        service = StudioService(root)
        app = create_studio_app(
            root,
            service=service,
            session_token=session_token,
            csrf_token=csrf_token,
            security_enabled=os.environ.get("KSADK_STUDIO_NO_SECURITY") != "1",
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
    finally:
        for key in managed_keys:
            if key in previously_present:
                os.environ[key] = previous[key] or ""
            else:
                os.environ.pop(key, None)
