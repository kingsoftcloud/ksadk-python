"""
agentengine model - 切换模型
"""

import os
import click
import httpx
import questionary
import yaml
from pathlib import Path
from dotenv import set_key
from ksadk.deployment.state import load_state
from ksadk.cli.error_utils import abort_with_cli_error, print_exception, usage_error
from ksadk.cli.resource_common import CONTEXT_SETTINGS, CompatibilityAliasCommand, print_compatibility_hint
from ksadk.cli.ui import (
    is_color_disabled,
    is_stdout_tty,
    print_error,
    print_info,
    print_kv,
    print_success,
    print_title,
    print_warn,
)


def _parse_model_selection(raw: str | None) -> list[str]:
    items: list[str] = []
    for part in str(raw or "").replace(";", ",").split(","):
        item = part.strip()
        if item and item not in items:
            items.append(item)
    return items


def _detect_framework_from_cwd(cwd: Path | None = None) -> str:
    root = cwd or Path.cwd()
    state = load_state(root)
    framework = str(state.get("framework") or state.get("type") or "").strip().lower()
    if framework:
        return framework
    for file_name in ("agentengine.yaml", "ksadk.yaml"):
        config_path = root / file_name
        if not config_path.exists():
            continue
        try:
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        framework = str(payload.get("framework") or "").strip().lower()
        if framework:
            return framework
    return ""


def _model_allowlist_env_key(framework: str | None) -> str:
    if str(framework or "").strip().lower() == "openclaw":
        return "OPENCLAW_MODEL_ALLOWLIST"
    return "AGENTENGINE_MODEL_ALLOWLIST"


def _resolve_env_file() -> Path:
    path = Path.cwd() / ".env"
    if not path.exists():
        print_warn("未找到当前项目 .env 文件，将在当前目录创建")
        path.touch()
    return path


def _write_default_model(selected_model: str) -> Path:
    if not selected_model:
        raise click.ClickException("未选择模型")
    env_file = _resolve_env_file()
    success, _key, _value = set_key(env_file, "OPENAI_MODEL_NAME", selected_model, quote_mode="never")
    if not success:
        raise click.ClickException("更新 OPENAI_MODEL_NAME 失败")
    return env_file


def _write_model_allowlist(
    *,
    selected_models: list[str],
    framework: str | None = None,
) -> tuple[Path, str | None]:
    env_file = _write_default_model(selected_models[0] if selected_models else "")
    if len(selected_models) <= 1:
        return env_file, None
    resolved_framework = framework or _detect_framework_from_cwd()
    allowlist_key = _model_allowlist_env_key(resolved_framework)
    success, _key, _value = set_key(
        env_file,
        allowlist_key,
        ",".join(selected_models),
        quote_mode="never",
    )
    if not success:
        raise click.ClickException(f"更新 {allowlist_key} 失败")
    return env_file, allowlist_key


def _build_model_env_pairs(
    *,
    selected_models: list[str],
    framework: str | None = None,
) -> list[tuple[str, str]]:
    if not selected_models:
        raise click.ClickException("未选择模型")
    pairs = [("OPENAI_MODEL_NAME", selected_models[0])]
    if len(selected_models) > 1:
        resolved_framework = framework or _detect_framework_from_cwd()
        pairs.append((_model_allowlist_env_key(resolved_framework), ",".join(selected_models)))
    return pairs


def run_model_command(
    *,
    compatibility_alias: bool = False,
    multi: bool = False,
    env_models: str | None = None,
    framework: str | None = None,
):
    """切换默认模型 (修改 .env)

    从 OPENAI_BASE_URL 获取可用模型列表，并更新 .env 中的 OPENAI_MODEL_NAME
    """
    selected_for_env = _parse_model_selection(env_models)
    if selected_for_env:
        for key, value in _build_model_env_pairs(
            selected_models=selected_for_env,
            framework=None if framework == "auto" else framework,
        ):
            click.echo(f"{key}={value}")
        return

    if compatibility_alias:
        print_compatibility_hint(
            legacy="agentengine model",
            canonical="agentengine config model",
        )

    if not is_stdout_tty():
        abort_with_cli_error(
            usage_error(
                "`agentengine config model` 需要交互式终端 (TTY)。",
                hints=[
                    "查看当前模型配置请使用 `agentengine config show`。",
                    "非交互修改请使用 `agentengine config set OPENAI_MODEL_NAME=<model>`。",
                    "为 Agent/deploy 生成环境变量请使用 `agentengine config model --env <model[,model...]>`。",
                ],
            ),
            argv=["config", "model"],
        )
    # 智能初始化 (加载 .env + 默认配置，支持自动推导 API Key/Base)
    from ksadk.configs import setup_environment

    setup_environment(Path.cwd())
    print_title("模型切换")

    # 支持两种环境变量名 (OPENAI_BASE_URL 优先, OPENAI_API_BASE 兼容旧版)
    api_base = os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE")
    api_key = os.getenv("OPENAI_API_KEY")
    current_model = os.getenv("OPENAI_MODEL_NAME") or os.getenv("MODEL_NAME")  # 兼容旧版

    if not api_base:
        print_error("未找到 OPENAI_BASE_URL")
        print_info("请先在 .env 文件中配置 API 地址 (OPENAI_BASE_URL)")
        return

    # 有些兼容接口可能不需要 Key，但通常都需要
    if not api_key:
        # 尝试匿名访问或提示 warning
        pass

    print_kv("正在获取模型列表", api_base, value_style="#58a6ff")

    try:
        # 处理 API Base URL，防止重复添加 /v1
        base_url = api_base.rstrip("/")
        if base_url.endswith("/v1"):
            url = f"{base_url}/models"
        else:
            url = f"{base_url}/v1/models"

        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # 增加 verify=False 以防自签证书 (和 invoke -k 保持一致比较好，但这里默认开启验证)
        resp = httpx.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        # Parse models
        # OpenAI format: {"data": [{"id": "model-id", ...}, ...]}
        # 兼容某些非标准接口可能直接返回 list
        if isinstance(data, list):
            models = [m["id"] if isinstance(m, dict) else m for m in data]
        else:
            models = [m["id"] for m in data.get("data", [])]

        models.sort()

        if not models:
            print_warn("接口返回了空模型列表")
            return

        # Mark current model in the list
        choices = []
        default_choice = None

        for m in models:
            if m == current_model:
                # 使用 kwargs 避免参数顺序错误
                c = questionary.Choice(title=f"{m} (当前)", value=m)
                choices.append(c)
                default_choice = c  # 记录同一个对象引用
            else:
                choices.append(m)

        style = (
            None
            if is_color_disabled()
            else questionary.Style(
                [
                    ("qmark", "fg:green bold"),
                    ("question", "bold"),
                    ("answer", "fg:green"),
                    ("pointer", "fg:cyan bold"),
                    ("highlighted", "fg:cyan bold"),
                ]
            )
        )
        if multi:
            selected = questionary.checkbox(
                "Select models:",
                choices=choices,
                style=style,
            ).ask()
        else:
            # 构建选项列表
            # Questionary 默认支持按键搜索
            selected = questionary.select(
                "Select model:",
                choices=choices,
                default=default_choice,
                style=style,
            ).ask()

        if selected:
            selected_models = selected if isinstance(selected, list) else [selected]
            if not multi and selected_models[0] == current_model:
                print_success(f"模型未变更 ({selected})")
            else:
                if multi:
                    env_file, allowlist_key = _write_model_allowlist(
                        selected_models=selected_models,
                        framework=None if framework == "auto" else framework,
                    )
                else:
                    env_file = _write_default_model(selected_models[0])
                    allowlist_key = None
                print_success(f"已切换模型为: {selected_models[0]}")
                if allowlist_key:
                    print_success(f"已更新模型 allowlist: {allowlist_key}")
                print_info(f"已更新 {env_file}")

    except Exception as e:
        print_exception("获取模型失败", e)
        if "401" in str(e):
            print_info("提示: 请检查 OPENAI_API_KEY 是否正确")
        elif "404" in str(e):
            print_info("提示: 接口地址可能不正确，请检查 /v1/models 是否存在")


@click.command(
    context_settings=CONTEXT_SETTINGS,
    hidden=True,
    cls=CompatibilityAliasCommand,
    canonical_command="agentengine config model",
)
@click.option("--multi", is_flag=True, help="交互式多选模型，并按当前框架写入模型 allowlist")
@click.option(
    "--env",
    "env_models",
    default=None,
    help="按模型列表生成环境变量，逗号分隔；首个模型作为默认模型，不写入 .env",
)
@click.option(
    "--framework",
    type=click.Choice(["auto", "openclaw", "hermes", "generic"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="allowlist 变量选择策略；auto 会读取当前目录框架",
)
def model(multi: bool, env_models: str | None, framework: str):
    """切换默认模型 (兼容入口)。"""
    run_model_command(
        compatibility_alias=True,
        multi=multi,
        env_models=env_models,
        framework=framework,
    )
