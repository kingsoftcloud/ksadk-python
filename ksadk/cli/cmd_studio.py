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

# 模型环境变量白名单。OPENAI_BASE_URL 与 OPENAI_API_BASE 互为别名，两者都接受；
# 加载时做别名归一（见 studio()），运行时统一 OPENAI_BASE_URL 优先（与 cmd_config/cmd_model
# /api.py 一致，方案 §2.4 第 5 点）。
_MODEL_ENV_KEYS = (
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_API_KEY",
    "OPENAI_MODEL_NAME",
)
# Cloud-control credentials are intentionally process-only as well.  Studio
# needs them to use the Server Action API for a deployed Agent, but they must
# never become browser settings, workspace files, or runtime environment.
_CLOUD_CONTROL_ENV_KEYS = (
    "KSYUN_ACCESS_KEY",
    "KSYUN_SECRET_KEY",
    "KSYUN_REGION",
    "AGENTENGINE_REGION",
    "AGENTENGINE_SERVER_URL",
    "AGENTENGINE_STREAM_SERVER_URL",
    "AGENTENGINE_SIGN_SERVICE",
    "KS3_BUCKET",
    "KS3_ACCESS_KEY",
    "KS3_SECRET_KEY",
)
_STUDIO_ENV_FILE_KEYS = (*_MODEL_ENV_KEYS, *_CLOUD_CONTROL_ENV_KEYS)
# 别名归一：两者任一有值时，把另一个也设上，保证下游无论读哪个都命中。
_MODEL_BASE_URL_ALIASES = ("OPENAI_BASE_URL", "OPENAI_API_BASE")


@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.argument("workspace", default=".", type=click.Path())
@click.option("--port", "-p", default=8080, type=click.IntRange(1, 65535))
@click.option("--no-open", is_flag=True, help="仅打印 URL，不自动打开浏览器")
@click.option(
    "--env-file",
    type=click.Path(exists=True, dir_okay=False),
    help=(
        "本地模型与云端控制环境文件；只读取允许的 OPENAI/KSYUN/KS3 "
        "字段，且仅保留在 Studio 进程"
    ),
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
    managed_keys = (*_STUDIO_ENV_FILE_KEYS, "KSADK_CODEX_USE_PROXY")
    previous = {key: os.environ.get(key) for key in managed_keys}
    previously_present = {key for key in managed_keys if key in os.environ}
    try:
        if env_file:
            try:
                values = load_env_file(env_file)
            except ValueError as exc:
                raise click.ClickException(str(exc)) from exc
            loaded_models = 0
            loaded_cloud_control = 0
            for key, value in values.items():
                if key not in _STUDIO_ENV_FILE_KEYS or not value:
                    continue
                if key in _MODEL_ENV_KEYS:
                    loaded_models += 1
                else:
                    loaded_cloud_control += 1
                # An explicit --env-file is the operator's selected cloud
                # identity.  Do not silently reuse inherited AK/SK from the
                # shell, which can point Studio at another tenant.  Model
                # values keep their historical shell-first precedence.
                if key in _CLOUD_CONTROL_ENV_KEYS or key not in os.environ:
                    os.environ[key] = value
            # 别名归一（方案 §2.4 第 5 点）：OPENAI_BASE_URL 与 OPENAI_API_BASE 互为别名。
            # 加载后任一有值则把另一个也设上，保证下游无论读哪个都命中；OPENAI_BASE_URL 优先。
            resolved_base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get(
                "OPENAI_API_BASE"
            )
            if resolved_base_url:
                os.environ["OPENAI_BASE_URL"] = resolved_base_url
                os.environ["OPENAI_API_BASE"] = resolved_base_url
                # base_url 至少算一次，避免显示 0/4 误导。
                loaded_models = max(loaded_models, 2)
            print_kv(
                "模型环境",
                f"已安全加载 {loaded_models}/{len(_MODEL_ENV_KEYS)} 个字段",
            )
            if loaded_cloud_control:
                print_kv(
                    "云端控制",
                    "已安全加载 "
                    f"{loaded_cloud_control}/{len(_CLOUD_CONTROL_ENV_KEYS)} 个字段"
                    "（仅本地进程）",
                )
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
