"""agentengine hermes - Hermes resource management."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, cast

import click
from click.core import ParameterSource

from ksadk.api import AgentEngineClient
from ksadk.cli.agent_ref import merge_agent_inputs, resolve_agent_ref
from ksadk.cli.cmd_dashboard import _open_dashboard
from ksadk.cli.dry_run import dry_run_option, effective_dry_run, run_async_with_dry_run
from ksadk.cli.env_options import (
    apply_explicit_env_with_shell_priority,
    env_options,
    inject_env_to_environ,
    resolve_runtime_env_overrides,
)
from ksadk.cli.error_utils import remote_error, resolution_error
from ksadk.cli.model_catalog import fetch_provider_model_metadata
from ksadk.cli.network_options import build_network_payload, network_cli_kwargs, network_options
from ksadk.cli.resource_common import (
    CONTEXT_SETTINGS,
    ResourceActionSet,
    ResourceDescriptor,
    ResourceListSchema,
    ResourceStatusSchema,
    build_dry_run_envelope,
    build_result_envelope,
    confirm_destructive,
    confirm_options,
    pagination_options,
    print_next_action_hint,
    render_descriptor_list,
    render_descriptor_status,
)
from ksadk.cli.storage import build_storage_config
from ksadk.cli.ui import (
    emit_json,
    is_json_output,
    print_info,
    print_kv,
    print_success,
    print_title,
    print_warn,
    status_rich_style,
)
from ksadk.cli.ui import (
    output_option as cli_output_option,
)
from ksadk.configs.env_registry import is_sensitive_env_var
from ksadk.deployment.agent_access import (
    get_latest_agent_access,
    is_agent_not_found_error,
    normalize_deployment_status,
)
from ksadk.deployment.state import clear_state, load_state, save_state
from ksadk.hermes_terminal import (
    run_hermes_terminal_session,
    validate_hermes_pairing_argv,
)
from ksadk.model_policy import build_runtime_model_policy_env

DEFAULT_HERMES_IMAGE = "ghcr.io/kingsoftcloud/hermes-agent:v2026.7.7.2-ksadk-v070"
DEFAULT_HERMES_CONTEXT_LENGTHS = (("glm-5.1", "200000"),)
DEFAULT_HERMES_MODEL_NAME = "glm-5.2"
DEFAULT_HERMES_PUBLIC_BASE_URL = "https://kspmas.ksyun.com/v1/"
DEFAULT_HERMES_RUNTIME_BASE_URL = DEFAULT_HERMES_PUBLIC_BASE_URL
KSPMAS_PUBLIC_BASES = (
    "http://kspmas.ksyun.com",
    "https://kspmas.ksyun.com",
)
KSPMAS_INTERNAL_BASE = DEFAULT_HERMES_PUBLIC_BASE_URL.rstrip("/")
_HERMES_GLOBAL_ENV_CACHE: dict[str, str] | None = None

HERMES_RESOURCE = ResourceDescriptor(
    name="Hermes",
    summary="Hermes Agent 资源管理。",
    resource_key="hermes",
    actions=ResourceActionSet(
        deploy="agentengine hermes deploy",
        list="agentengine hermes list",
        status="agentengine hermes status [agent_ref]",
        delete="agentengine hermes delete <agent_ref...>",
        open="agentengine hermes open [agent_ref]",
        extra=("connect", "exec", "pairing"),
    ),
    list_schema=ResourceListSchema(
        title="Hermes 实例列表",
        noun="Hermes 实例",
        columns=(
            {"header": "ID", "key": "id", "style": "#58a6ff", "no_wrap": True},
            {"header": "名称", "key": "name", "no_wrap": True},
            {"header": "状态", "key": "status", "no_wrap": True},
            {"header": "Endpoint", "key": "endpoint", "overflow": "fold"},
            {"header": "区域", "key": "region", "no_wrap": True},
        ),
        empty_message="没有找到 Hermes 实例",
    ),
    status_schema=ResourceStatusSchema(
        title="Hermes 状态",
        next_steps=(
            "agentengine invoke <agent>        # 原生 Hermes TUI",
            "agentengine hermes connect <agent>  # 远端配置 Feishu/Weixin，gateway 由容器托管",
            "agentengine hermes open <agent> --chat",
            "agentengine hermes exec <agent> -- status",
            "agentengine hermes pairing <agent> -- list",
        ),
    ),
    examples=(
        "agentengine hermes deploy --name demo-hermes",
        "agentengine hermes list",
        "agentengine hermes status ar-xxxx",
        "agentengine hermes connect ar-xxxx",
        "agentengine hermes open ar-xxxx --manage",
        "agentengine hermes open ar-xxxx --chat",
        "agentengine hermes exec ar-xxxx -- status",
        "agentengine hermes pairing ar-xxxx -- list",
        "agentengine hermes pairing ar-xxxx -- approve wpsxiezuo <code>",
        "agentengine hermes delete ar-xxxx",
    ),
    missing_ref_message="未找到 Hermes Agent，请指定 Agent（--agent 或位置参数）",
    resolution_commands=("agentengine hermes list",),
)


def _option_was_explicit(ctx: click.Context | None, name: str) -> bool:
    if ctx is None:
        return False
    try:
        return ctx.get_parameter_source(name) != ParameterSource.DEFAULT
    except Exception:
        return False


def _build_hermes_update_payload(
    *,
    payload: dict[str, Any],
    storage_config: dict[str, Any] | None,
    network_payload: dict[str, Any] | None,
    include_env: bool,
    include_storage: bool,
) -> dict[str, Any]:
    """构建已有 Hermes 的最小更新请求，避免镜像更新覆盖用户配置。"""
    update_payload: dict[str, Any] = {
        "name": payload["name"],
        "description": payload["description"],
        "framework": payload["framework"],
        "artifact_type": payload["artifact_type"],
        "artifact_path": payload["artifact_path"],
        "region": payload["region"],
        "resources": payload["resources"],
        "scaling": payload["scaling"],
        "ui_config": payload["ui_config"],
        "enable_observability": payload["enable_observability"],
    }
    if include_env:
        update_payload["env_vars"] = payload["env_vars"]
    if include_storage and storage_config:
        update_payload["storage"] = storage_config
    if network_payload:
        update_payload["network"] = network_payload
    return update_payload


@click.group("hermes", context_settings=CONTEXT_SETTINGS)
def hermes():
    """Hermes Agent 资源管理。"""


def _get_hermes_global_env() -> dict[str, str]:
    global _HERMES_GLOBAL_ENV_CACHE
    if _HERMES_GLOBAL_ENV_CACHE is not None:
        return _HERMES_GLOBAL_ENV_CACHE
    try:
        from ksadk.configs.global_config import get_env_from_global_config

        _HERMES_GLOBAL_ENV_CACHE = {
            str(key): str(value).strip()
            for key, value in get_env_from_global_config().items()
            if key and value is not None and str(value).strip()
        }
    except Exception:
        _HERMES_GLOBAL_ENV_CACHE = {}
    return _HERMES_GLOBAL_ENV_CACHE


def _env_value(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    global_env = _get_hermes_global_env()
    for name in names:
        value = global_env.get(name)
        if value:
            return value
    return ""


def _normalize_hermes_ui_locale(raw: Optional[str]) -> str:
    """标准化 Hermes UI 语言代码，当前 upstream 只支持 en / zh。"""
    text = str(raw or "").strip()
    if not text:
        return "zh"

    base = text.split(".", 1)[0].replace("_", "-").strip().lower()
    if base in {"c", "c-utf-8", "c.utf-8", "posix"}:
        return "zh"
    if base.startswith("en"):
        return "en"
    if base.startswith("zh"):
        return "zh"
    return "zh"


async def _fetch_hermes_bootstrap_config(region: str) -> dict[str, Any] | None:
    """从服务端获取 Hermes 客户端启动配置。失败时返回 None。"""
    from ksadk.version import VERSION as CLI_VERSION

    try:
        async with AgentEngineClient(region=region) as client:
            return cast(
                dict[str, Any],
                await client.get_client_bootstrap_config(
                    product="hermes",
                    framework="hermes",
                    region=region,
                    client_type="cli",
                    client_version=CLI_VERSION,
                    locale=_env_value("LANG", "LC_ALL"),
                    ignore_dry_run=True,
                ),
            )
    except Exception as e:
        print_warn(f"拉取 Hermes 服务端默认配置失败，回退本地默认镜像: {e}")
        return None


def _extract_hermes_bootstrap_image(bootstrap_cfg: dict[str, Any] | None) -> str:
    if not isinstance(bootstrap_cfg, dict):
        return ""
    configs = bootstrap_cfg.get("configs") or bootstrap_cfg.get("Configs")
    if not isinstance(configs, dict):
        return ""
    value = configs.get("bootstrap.default_image") or configs.get("hermes.default_image")
    return str(value or "").strip()


def _default_context_length_for_model(model: str | None) -> str:
    normalized = str(model or "").strip().lower()
    if not normalized:
        return ""
    for model_fragment, context_length in DEFAULT_HERMES_CONTEXT_LENGTHS:
        if model_fragment in normalized:
            return context_length
    return ""


def _build_hermes_env_vars(
    *,
    model_base_url: str | None = None,
    model_api_key: str | None = None,
    default_model: str | None = None,
    model_metadata: dict[str, Any] | None = None,
    cli_env: dict[str, str] | None = None,
    auto_dotenv: dict[str, str] | None = None,
    shell_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    raw_model_base_url = model_base_url or _env_value("OPENAI_BASE_URL")
    resolved_model_base_url = (
        _normalize_hermes_runtime_base_url(raw_model_base_url)
        if raw_model_base_url
        else DEFAULT_HERMES_RUNTIME_BASE_URL
    )
    resolved_default_model = (
        default_model or _env_value("OPENAI_MODEL_NAME") or DEFAULT_HERMES_MODEL_NAME
    )
    metadata_context_length = ""
    if isinstance(model_metadata, dict):
        metadata_context_length = str(model_metadata.get("context_window_tokens") or "").strip()
    context_length = (
        _env_value("HERMES_CONTEXT_LENGTH", "OPENAI_CONTEXT_LENGTH", "MODEL_CONTEXT_LENGTH")
        or metadata_context_length
        or _default_context_length_for_model(resolved_default_model)
    )
    ui_locale = _normalize_hermes_ui_locale(_env_value("HERMES_UI_LOCALE", "LANG", "LC_ALL"))
    raw = {
        "OPENAI_API_KEY": model_api_key or _env_value("OPENAI_API_KEY"),
        "OPENAI_BASE_URL": resolved_model_base_url,
        "OPENAI_MODEL_NAME": resolved_default_model,
        "API_SERVER_ENABLED": "true",
        "API_SERVER_HOST": "127.0.0.1",
        "API_SERVER_PORT": "8642",
        "HERMES_DASHBOARD_HOST": "127.0.0.1",
        "HERMES_DASHBOARD_PORT": "9119",
        "KSADK_RUNTIME_PORT": _env_value("PORT") or "8080",
        "HERMES_UI_LOCALE": ui_locale,
    }
    if context_length:
        raw["HERMES_CONTEXT_LENGTH"] = context_length
    fallback_model = _env_value("HERMES_FALLBACK_MODEL", "OPENAI_FALLBACK_MODEL_NAME")
    if fallback_model:
        raw["HERMES_FALLBACK_PROVIDER"] = _env_value("HERMES_FALLBACK_PROVIDER") or "custom"
        raw["HERMES_FALLBACK_MODEL"] = fallback_model
        raw["HERMES_FALLBACK_BASE_URL"] = (
            _env_value("HERMES_FALLBACK_BASE_URL") or resolved_model_base_url
        )
    api_server_key = _env_value("API_SERVER_KEY", "HERMES_API_SERVER_KEY")
    if api_server_key:
        raw["API_SERVER_KEY"] = api_server_key
    # Observability routes and credentials are platform-managed. The Hermes
    # deploy CLI must not translate or forward legacy Langfuse SDK variables;
    # server/runtime inject the standard OTLP primary and CloudMonitor secondary.
    for key in (
        "WPSXIEZUO_APP_ID",
        "WPSXIEZUO_APP_KEY",
        "WPSXIEZUO_API_BASE",
        "WPSXIEZUO_WS_ENDPOINT",
        "WPSXIEZUO_GROUP_AT_ONLY",
        "WPSXIEZUO_ALLOWED_USERS",
        "WPSXIEZUO_ALLOW_ALL_USERS",
        "WPSXIEZUO_HOME_CHANNEL",
    ):
        value = _env_value(key)
        if value:
            raw[key] = value
    raw = build_runtime_model_policy_env(raw, runtime="hermes")
    if raw.get("HERMES_FALLBACK_MODEL"):
        raw.setdefault(
            "HERMES_FALLBACK_PROVIDER", _env_value("HERMES_FALLBACK_PROVIDER") or "custom"
        )
        raw.setdefault(
            "HERMES_FALLBACK_BASE_URL",
            _env_value("HERMES_FALLBACK_BASE_URL") or resolved_model_base_url,
        )
    if cli_env or auto_dotenv:
        apply_explicit_env_with_shell_priority(
            raw, cli_env or {}, auto_dotenv or {}, shell_keys or set(os.environ)
        )
    return [
        {
            "Key": key,
            "Value": str(value),
            "IsSensitive": is_sensitive_env_var(key),
        }
        for key, value in raw.items()
        if value is not None and str(value).strip() != ""
    ]


def _validate_hermes_model_config(
    *,
    model_base_url: str | None = None,
    model_api_key: str | None = None,
    default_model: str | None = None,
) -> None:
    _ = model_api_key
    resolved_model_base_url = model_base_url or _env_value("OPENAI_BASE_URL")
    resolved_default_model = default_model or _env_value("OPENAI_MODEL_NAME")
    if not resolved_model_base_url:
        print_info(f"未配置 OPENAI_BASE_URL，默认使用: {DEFAULT_HERMES_PUBLIC_BASE_URL}")
    if not resolved_default_model:
        print_info(f"未配置 OPENAI_MODEL_NAME，默认使用: {DEFAULT_HERMES_MODEL_NAME}")
    if not (model_api_key or _env_value("OPENAI_API_KEY")):
        print_info("未配置 OPENAI_API_KEY，将由服务端在需要时自动创建。")


def _normalize_hermes_runtime_base_url(base_url: str | None) -> str:
    normalized = str(base_url or "").strip()
    return normalized


_FAILURE_STATUSES = {"FAILED", "ERROR", "TERMINATED"}


def _diagnostic_field_style(status_value: str) -> str:
    """非 RUNNING 诊断行样式：失败用红色，其余用黄色。"""
    return "bold #f85149" if status_value in _FAILURE_STATUSES else "bold #d29922"


def _flatten_agent_detail(agent: dict[str, Any]) -> dict[str, Any]:
    basic_value = agent.get("basic")
    deployment_value = agent.get("deployment")
    quick_value = agent.get("quick_access")
    basic = basic_value if isinstance(basic_value, dict) else {}
    deployment = deployment_value if isinstance(deployment_value, dict) else {}
    quick = quick_value if isinstance(quick_value, dict) else {}
    return {
        "agent_id": basic.get("agent_id") or agent.get("agent_id"),
        "name": basic.get("name") or agent.get("name"),
        "status": basic.get("status") or agent.get("status") or "UNKNOWN",
        "phase": basic.get("phase") or "",
        "message": basic.get("message") or "",
        "replicas": basic.get("replicas"),
        "ready_replicas": basic.get("ready_replicas"),
        "framework": deployment.get("framework")
        or basic.get("framework")
        or agent.get("framework"),
        "region": basic.get("region") or agent.get("region"),
        "endpoint": quick.get("public_endpoint")
        or quick.get("private_endpoint")
        or agent.get("endpoint"),
        "api_key": quick.get("api_key") or agent.get("api_key"),
        "artifact_path": deployment.get("artifact_path") or agent.get("artifact_path"),
        "langfuse_url": (agent.get("advanced") or {}).get("observability_url")
        or agent.get("langfuse_trace_url")
        or "",
    }


def _resolve_hermes_ref(agent_ref: str | None) -> str:
    resolved = resolve_agent_ref(
        agent_ref, cwd=Path(".").resolve(), include_state=True, include_project_config=True
    )
    if not resolved:
        raise resolution_error(
            HERMES_RESOURCE.missing_ref_message or "请指定 Hermes Agent。",
            hints=list(HERMES_RESOURCE.resolution_commands),
        )
    if resolved.source != "cli":
        print_info(f"未显式指定 Hermes，使用 {resolved.source_text}: {resolved.value}")
    return resolved.value


async def _get_hermes_detail_with_client(
    client: AgentEngineClient,
    agent_ref: str,
    *,
    include_api_key: bool = False,
) -> dict[str, Any]:
    if str(agent_ref).startswith("ar-"):
        agent = await client.get_agent(agent_id=agent_ref, include_api_key=include_api_key)
    else:
        agent = await client.get_agent(name=agent_ref, include_api_key=include_api_key)
    detail = _flatten_agent_detail(agent)
    framework = str(detail.get("framework") or "").strip().lower()
    if framework and framework != "hermes":
        raise resolution_error(
            f"目标 Agent 不是 Hermes: {agent_ref}", hints=["agentengine hermes list"]
        )
    return detail


async def _get_hermes_detail(
    region: str, agent_ref: str, *, include_api_key: bool = False
) -> dict[str, Any]:
    async with AgentEngineClient(region=region) as client:
        return await _get_hermes_detail_with_client(
            client, agent_ref, include_api_key=include_api_key
        )


def _resolve_hermes_access(
    *,
    agent_ref: str | None,
    region: str,
    endpoint: str | None = None,
    api_key: str | None = None,
) -> dict[str, str | None]:
    if endpoint:
        return {"endpoint": endpoint, "api_key": api_key}
    resolved = _resolve_hermes_ref(agent_ref)
    detail = asyncio.run(_get_hermes_detail(region, resolved, include_api_key=True))
    endpoint_value = str(detail.get("endpoint") or "").strip()
    if not endpoint_value:
        raise resolution_error(
            "目标 Hermes Agent 未返回 Endpoint", hints=["agentengine hermes status <agent>"]
        )
    return {
        "endpoint": endpoint_value,
        "api_key": api_key or detail.get("api_key"),
        "agent_id": detail.get("agent_id"),
        "name": detail.get("name"),
    }


def _split_terminal_agent_ref_and_argv(
    argv: tuple[str, ...],
    *,
    validator: Callable[[tuple[str, ...] | list[str]], list[str]],
) -> tuple[str | None, list[str]]:
    """Split argv into (agent_ref, exec_argv).

    The first token is treated as agent_ref when the validator rejects the
    full argv (whitelist validators raise ValueError), or when the validator
    is the passthrough one and the first token looks like an agent id
    (``ar-`` prefix) with remaining tokens.
    """
    raw = [str(item) for item in argv]
    try:
        return None, validator(raw)
    except ValueError as direct_error:
        if len(raw) >= 2:
            try:
                return raw[0], validator(raw[1:])
            except ValueError:
                pass
        raise direct_error


def _passthrough_exec_argv(argv: tuple[str, ...] | list[str]) -> list[str]:
    """Local exec passes argv through unchanged; the pod enforces allowlists.

    Raises ValueError when the first token looks like an agent id (``ar-``
    prefix) and there are remaining tokens, so ``_split_terminal_agent_ref_and_argv``
    falls back to splitting the first token as agent_ref.
    """
    items = [str(item) for item in argv]
    if len(items) >= 2 and items[0].startswith("ar-"):
        raise ValueError("first token looks like an agent ref")
    return items


def _render_hermes_dry_run(
    action: str, request: dict[str, Any], hints: tuple[str, ...] = ()
) -> None:
    if is_json_output():
        emit_json(
            build_dry_run_envelope(
                resource="hermes",
                action=action,
                request=request,
                hints=list(hints),
            )
        )
        return
    print_title("Hermes Dry Run", f"action: {action}")
    for key, value in request.items():
        if isinstance(value, (list, tuple)):
            rendered = " ".join(str(item) for item in value) or "-"
        else:
            rendered = str(value if value is not None else "-")
        print_kv(key, rendered)
    for hint in hints:
        print_info(hint)


@hermes.command("deploy", context_settings=CONTEXT_SETTINGS)
@click.option("--name", "-n", default=None, help="Hermes Agent 名称")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--image", default=None, help="Hermes runtime 镜像地址")
@click.option("--model-base-url", default=None, help="模型 Base URL (默认 OPENAI_BASE_URL)")
@click.option("--model-api-key", default=None, help="模型 API Key (默认 OPENAI_API_KEY)")
@click.option("--default-model", default=None, help="默认模型名 (默认 OPENAI_MODEL_NAME)")
@click.option("--cpu", default="2", help="CPU 规格")
@click.option("--memory", default="4Gi", help="内存规格")
@click.option("--storage-size-gi", type=int, default=20, show_default=True, help="PVC 容量（Gi）")
@click.option("--storage-mount-path", default=None, help="PVC 挂载目录（默认: /home/node/.hermes）")
@click.option("--no-storage", is_flag=True, help="禁用默认 PVC 挂载")
@env_options
@click.option(
    "--observability/--no-observability",
    default=True,
    help="是否启用可观测性 (默认开启)",
)
@click.option(
    "--agent-id",
    "agent_id_opt",
    default=None,
    help=(
        "指定要更新的已有 Agent ID；当前凭证有权限时会自动回填 "
        ".agentengine.state 并走热更新（用于本地状态丢失后重新关联）"
    ),
)
@network_options
@dry_run_option()
@cli_output_option()
def deploy(
    name: Optional[str],
    region: str,
    image: Optional[str],
    model_base_url: Optional[str],
    model_api_key: Optional[str],
    default_model: Optional[str],
    cpu: str,
    memory: str,
    storage_size_gi: int,
    storage_mount_path: Optional[str],
    no_storage: bool,
    extra_env: tuple[str, ...],
    env_file: Optional[str],
    observability: bool,
    agent_id_opt: Optional[str],
    enable_public_access: Optional[bool],
    enable_vpc_access: bool,
    vpc_id: Optional[str],
    subnet_id: Optional[str],
    security_group_id: Optional[str],
    availability_zone: Optional[str],
    dry_run: bool,
    output_mode: str | None,
):
    """部署 Hermes runtime 到云端。"""
    _ = output_mode
    dry_run = effective_dry_run(dry_run)
    ctx = click.get_current_context(silent=True)
    include_env_on_update = any(
        (
            bool(extra_env),
            _option_was_explicit(ctx, "env_file"),
            _option_was_explicit(ctx, "model_base_url"),
            _option_was_explicit(ctx, "model_api_key"),
            _option_was_explicit(ctx, "default_model"),
        )
    )
    include_storage_on_update = any(
        (
            _option_was_explicit(ctx, "storage_size_gi"),
            _option_was_explicit(ctx, "storage_mount_path"),
            _option_was_explicit(ctx, "no_storage"),
        )
    )
    run_async_with_dry_run(
        _deploy_hermes(
            name=name,
            region=region,
            image=image,
            model_base_url=model_base_url,
            model_api_key=model_api_key,
            default_model=default_model,
            cpu=cpu,
            memory=memory,
            storage_size_gi=storage_size_gi,
            storage_mount_path=storage_mount_path,
            no_storage=no_storage,
            observability=observability,
            agent_id=agent_id_opt,
            include_env_on_update=include_env_on_update,
            include_storage_on_update=include_storage_on_update,
            extra_env=extra_env,
            env_file=env_file,
            **network_cli_kwargs(
                enable_public_access=enable_public_access,
                enable_vpc_access=enable_vpc_access,
                vpc_id=vpc_id,
                subnet_id=subnet_id,
                security_group_id=security_group_id,
                availability_zone=availability_zone,
            ),
            dry_run=dry_run,
        ),
        dry_run=dry_run,
        dry_run_resource="hermes",
        dry_run_action="deploy",
    )


async def _deploy_hermes(
    *,
    name: str | None,
    region: str,
    image: str | None,
    model_base_url: str | None,
    model_api_key: str | None,
    default_model: str | None,
    cpu: str,
    memory: str,
    storage_size_gi: int,
    storage_mount_path: str | None,
    no_storage: bool,
    observability: bool,
    agent_id: str | None = None,
    include_env_on_update: bool,
    include_storage_on_update: bool,
    extra_env: tuple[str, ...] = (),
    env_file: str | None = None,
    enable_public_access: bool | None = None,
    enable_vpc_access: bool = False,
    vpc_id: str | None = None,
    subnet_id: str | None = None,
    security_group_id: str | None = None,
    availability_zone: str | None = None,
    dry_run: bool = False,
) -> None:
    project_dir = Path(".").resolve()
    cli_env, auto_dotenv, shell_keys, env_source = resolve_runtime_env_overrides(
        env_file=env_file,
        extra_env=extra_env,
        base_dir=project_dir,
    )
    loaded = inject_env_to_environ(cli_env, auto_dotenv, shell_keys)
    if loaded:
        print_info(f"已从 {env_source or '--env'} 注入环境变量: {loaded} 项")
    if cli_env or auto_dotenv:
        include_env_on_update = True
    state = load_state(project_dir)
    existing_agent_id = None
    if str(state.get("type") or state.get("framework") or "").strip().lower() == "hermes":
        existing_agent_id = str(state.get("agent_id") or "").strip() or None
    explicit_agent_id = (agent_id or "").strip() or None
    if explicit_agent_id:
        if existing_agent_id and existing_agent_id != explicit_agent_id:
            print_warn(
                f"--agent-id ({explicit_agent_id}) 与本地状态 "
                f"({existing_agent_id}) 不一致，以 --agent-id 为准"
            )
        existing_agent_id = explicit_agent_id
    agent_name = name or state.get("name") or project_dir.name.replace("-", "_")
    image_ref = image or _env_value("HERMES_IMAGE", "HERMES_DOCKER_IMAGE")
    if not image_ref:
        bootstrap_cfg = await _fetch_hermes_bootstrap_config(region)
        image_ref = _extract_hermes_bootstrap_image(bootstrap_cfg)
        if image_ref:
            print_info(f"未指定镜像，使用服务端默认镜像: {image_ref}")
    image_ref = image_ref or DEFAULT_HERMES_IMAGE
    _validate_hermes_model_config(
        model_base_url=model_base_url,
        model_api_key=model_api_key,
        default_model=default_model,
    )
    resolved_default_model = (
        default_model or _env_value("OPENAI_MODEL_NAME") or DEFAULT_HERMES_MODEL_NAME
    )
    model_metadata = await fetch_provider_model_metadata(
        api_base=model_base_url or _env_value("OPENAI_BASE_URL"),
        api_key=model_api_key or _env_value("OPENAI_API_KEY"),
        model=resolved_default_model,
    )
    env_vars = _build_hermes_env_vars(
        model_base_url=model_base_url,
        model_api_key=model_api_key,
        default_model=default_model,
        model_metadata=model_metadata,
        cli_env=cli_env,
        auto_dotenv=auto_dotenv,
        shell_keys=shell_keys,
    )
    payload = {
        "name": agent_name,
        "description": "Hermes Agent (managed by AgentEngine)",
        "framework": "hermes",
        "artifact_type": "Container",
        "artifact_path": image_ref,
        "region": region,
        "resources": {"cpu": cpu, "memory": memory},
        "scaling": {"min_replicas": 1, "max_replicas": 1, "concurrency": 1000},
        "enable_observability": observability,
        "env_vars": env_vars,
        "ui_config": {"profile": "hermes", "path": "/", "url": None},
    }
    storage_config = build_storage_config(
        "hermes",
        no_storage=no_storage,
        mount_path=storage_mount_path,
        size_gi=storage_size_gi,
    )
    if storage_config:
        payload["storage"] = storage_config
    if existing_agent_id and include_storage_on_update and no_storage:
        print_warn(
            "更新已有 Hermes 时 `--no-storage` 不会删除服务端既有挂盘配置；默认保留已有配置。"
        )
    network_payload = build_network_payload(
        enable_public_access=enable_public_access,
        enable_vpc_access=enable_vpc_access,
        vpc_id=vpc_id,
        subnet_id=subnet_id,
        security_group_id=security_group_id,
        availability_zone=availability_zone,
        region=region,
        dry_run=dry_run,
    )
    # create 默认开公网；update 使用原始 payload，None 表示保留现有配置。
    create_network_payload = dict(network_payload) if network_payload is not None else {}
    if "enable_public_access" not in create_network_payload:
        create_network_payload["enable_public_access"] = True
    if create_network_payload:
        payload["network"] = create_network_payload

    print_title("Hermes 云端部署", f"region: {region}")
    print_kv("名称", agent_name)
    print_kv("镜像", image_ref)

    async with AgentEngineClient(region=region, dry_run=dry_run) as client:
        if explicit_agent_id and not dry_run:
            try:
                detail = await client.get_agent(
                    explicit_agent_id, include_api_key=True
                )
            except Exception as e:
                raise click.ClickException(
                    f"指定的 Agent ID '{explicit_agent_id}' 不存在，或当前凭证无权限访问。\n"
                    f"   详情: {e}\n"
                    "   👉 请确认 agent_id 正确，且当前 AK/SK / 账号有该 Agent 的权限。"
                ) from e
            qa = detail.get("quick_access", {}) or {}
            basic = detail.get("basic", {}) or {}
            recovered_state = state.copy()
            recovered_state.update(
                {
                    "agent_id": explicit_agent_id,
                    "name": basic.get("name") or agent_name,
                    "type": "hermes",
                    "region": region,
                    "endpoint": qa.get("public_endpoint"),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            if qa.get("api_key"):
                recovered_state["api_key"] = qa["api_key"]
            recovered_state = {
                k: v for k, v in recovered_state.items() if v is not None
            }
            save_state(project_dir, recovered_state)
            state = recovered_state
            print_info(f"已通过 --agent-id 关联已有 Agent: {explicit_agent_id}")
        if existing_agent_id:
            update_payload = _build_hermes_update_payload(
                payload=payload,
                storage_config=storage_config,
                network_payload=network_payload,
                include_env=include_env_on_update,
                include_storage=include_storage_on_update,
            )
            try:
                res = await client.update_agent(existing_agent_id, update_payload)
            except Exception as update_err:
                if not is_agent_not_found_error(update_err):
                    raise
                # 本地 state 缓存的 agent_id 在服务端已不存在（已删除），
                # 清掉失效 state 后回退为新建，避免 404 卡住用户。
                print_warn(f"本地状态失效 ({existing_agent_id})，将自动回退为新建: {update_err}")
                cleared = clear_state(project_dir, key=existing_agent_id)
                if cleared:
                    print_info("已清理失效的 .agentengine.state")
                existing_agent_id = None
                res = None
            else:
                if res is None:
                    res = {}
                res.setdefault("agent_id", existing_agent_id)
                res.setdefault("endpoint", state.get("endpoint"))
                res.setdefault("api_key", state.get("api_key"))

        if not existing_agent_id:
            res = await client.create_agent(payload)
            if isinstance(res, dict):
                if res.get("order_id") and not res.get("agent_id"):
                    print_info(f"订单已创建: {res.get('order_id')}，等待 Hermes 实例创建...")
                    latest = await get_latest_agent_access(
                        client,
                        agent_name=agent_name,
                        attempts=12,
                        interval_seconds=5,
                        include_api_key=True,
                        detail_fetcher=lambda agent_ref, include_api_key: (
                            _get_hermes_detail_with_client(
                                client,
                                agent_ref,
                                include_api_key=include_api_key,
                            )
                        ),
                        suppress_transient_not_found_log=True,
                    )
                    if latest:
                        res = {
                            **res,
                            "agent_id": latest.get("agent_id"),
                            "name": latest.get("name") or agent_name,
                            "endpoint": latest.get("endpoint"),
                            "api_key": latest.get("api_key"),
                            "status": latest.get("status") or res.get("status"),
                        }
                    else:
                        print_warn("实例仍在创建中，稍后使用 `agentengine hermes status` 查看")
                elif res.get("agent_id") and (
                    not str(res.get("endpoint") or "").strip()
                    or not str(res.get("api_key") or "").strip()
                ):
                    latest = await get_latest_agent_access(
                        client,
                        agent_id=str(res.get("agent_id") or "").strip() or None,
                        attempts=5,
                        interval_seconds=1,
                        initial_delay_seconds=2,
                        require_complete_access=True,
                        include_api_key=True,
                        detail_fetcher=lambda agent_ref, include_api_key: (
                            _get_hermes_detail_with_client(
                                client,
                                agent_ref,
                                include_api_key=include_api_key,
                            )
                        ),
                        suppress_transient_not_found_log=True,
                    )
                    if latest:
                        res = {
                            **res,
                            "agent_id": latest.get("agent_id") or res.get("agent_id"),
                            "name": latest.get("name") or res.get("name") or agent_name,
                            "endpoint": latest.get("endpoint") or res.get("endpoint"),
                            "api_key": latest.get("api_key") or res.get("api_key"),
                            "status": latest.get("status") or res.get("status"),
                        }
    if dry_run:
        return

    final_agent_id = res.get("agent_id")
    endpoint = res.get("endpoint")
    api_key = res.get("api_key")
    deployment_status = normalize_deployment_status(res.get("status") or res.get("phase"))
    save_state(
        project_dir,
        {
            "type": "hermes",
            "framework": "hermes",
            "agent_id": final_agent_id,
            "name": res.get("name") or agent_name,
            "region": region,
            "endpoint": endpoint,
            "api_key": api_key,
            "image": image_ref,
            "ui_profile": "hermes",
            "ui_path": "/",
        },
    )
    if is_json_output():
        status_schema = HERMES_RESOURCE.status_schema
        emit_json(
            build_result_envelope(
                resource="hermes",
                action="deploy",
                result={
                    "id": str(final_agent_id or ""),
                    "agent_id": str(final_agent_id or ""),
                    "name": str(res.get("name") or agent_name),
                    "status": deployment_status,
                    "framework": "hermes",
                    "region": region,
                    "endpoint": str(endpoint or ""),
                    "image": image_ref,
                    "ui_profile": "hermes",
                    "ui_path": "/",
                },
                hints=list(status_schema.next_steps) if status_schema is not None else [],
            )
        )
        return
    print_success("Hermes 已提交部署")
    print_kv("Agent ID", str(final_agent_id or "(创建中)"))
    print_kv("当前状态", deployment_status, value_style=status_rich_style(deployment_status))
    if endpoint:
        print_kv("Endpoint", str(endpoint), value_style="#58a6ff")
    print_info("已保存状态到 .agentengine.state")
    print_next_action_hint(
        "agentengine hermes status",
        "agentengine hermes open --chat",
        "agentengine hermes connect",
    )


@hermes.command("list", context_settings=CONTEXT_SETTINGS)
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@pagination_options(default_page=1, default_size=20)
@dry_run_option()
@cli_output_option()
def list_hermes(region: str, page: int, size: int, dry_run: bool, output_mode: str | None):
    """列出 Hermes Agent。"""
    _ = output_mode
    dry_run = effective_dry_run(dry_run)

    async def _list():
        async with AgentEngineClient(region=region, dry_run=dry_run) as client:
            resp = await client.list_agents(
                region=region, framework="hermes", page=page, page_size=size
            )
        agents = resp.get("agents", []) or []
        rows = []
        items = []
        for agent in agents:
            detail = _flatten_agent_detail(agent)
            status = str(detail.get("status") or "UNKNOWN").upper()
            row = (
                str(detail.get("agent_id") or "-"),
                str(detail.get("name") or "-"),
                f"[{status_rich_style(status)}]{status}[/]",
                str(detail.get("endpoint") or "-"),
                str(detail.get("region") or region),
            )
            rows.append(row)
            items.append(
                {
                    "id": row[0],
                    "name": row[1],
                    "status": row[2],
                    "endpoint": row[3],
                    "region": row[4],
                }
            )
        render_descriptor_list(
            HERMES_RESOURCE,
            rows=rows,
            total=int(resp.get("total") or len(agents)),
            page=page,
            size=size,
            items=items,
        )

    run_async_with_dry_run(
        _list(), dry_run=dry_run, dry_run_resource="hermes", dry_run_action="list"
    )


@hermes.command("status", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@dry_run_option()
@cli_output_option()
def status(agent_ref: Optional[str], region: str, dry_run: bool, output_mode: str | None):
    """查看 Hermes Agent 状态。"""
    _ = output_mode
    dry_run = effective_dry_run(dry_run)
    resolved = _resolve_hermes_ref(agent_ref)

    async def _status():
        async with AgentEngineClient(region=region, dry_run=dry_run) as client:
            detail = await _get_hermes_detail_with_client(client, resolved)
        status_value = str(detail.get("status") or "UNKNOWN").upper()
        fields: list[tuple[str, str, str | None]] = [
            ("ID", str(detail.get("agent_id") or "-"), "#58a6ff"),
            ("状态", status_value, status_rich_style(status_value)),
        ]
        message = str(detail.get("message") or "").strip()
        if status_value != "RUNNING":
            replicas = detail.get("replicas")
            ready = detail.get("ready_replicas")
            if replicas is not None or ready is not None:
                ready_text = ready if ready is not None else "-"
                replicas_total_text = replicas if replicas is not None else "-"
                replicas_text = f"{ready_text}/{replicas_total_text}"
                replica_style = _diagnostic_field_style(status_value)
                fields.append(("副本", replicas_text, replica_style))
            if message:
                message_style = _diagnostic_field_style(status_value)
                fields.append(("消息", message, message_style))
        fields.extend(
            [
                ("框架", str(detail.get("framework") or "-"), None),
                ("区域", str(detail.get("region") or region), None),
                ("Endpoint", str(detail.get("endpoint") or "-"), "#58a6ff"),
                (
                    "Langfuse",
                    str(detail.get("langfuse_url") or "-"),
                    "#58a6ff" if detail.get("langfuse_url") else None,
                ),
                ("镜像", str(detail.get("artifact_path") or "-"), None),
            ]
        )
        render_descriptor_status(
            HERMES_RESOURCE,
            subtitle=str(detail.get("name") or resolved),
            fields=fields,
            item={
                "id": str(detail.get("agent_id") or "-"),
                "name": str(detail.get("name") or resolved),
                "status": status_value,
                "framework": str(detail.get("framework") or "-"),
                "region": str(detail.get("region") or region),
                "endpoint": str(detail.get("endpoint") or "-"),
                "langfuse_url": str(detail.get("langfuse_url") or ""),
                "image": str(detail.get("artifact_path") or "-"),
                "message": message,
                "phase": str(detail.get("phase") or ""),
                "replicas": detail.get("replicas"),
                "ready_replicas": detail.get("ready_replicas"),
            },
        )

    run_async_with_dry_run(
        _status(), dry_run=dry_run, dry_run_resource="hermes", dry_run_action="status"
    )


@hermes.command("open", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@click.option("--agent", "--agent-id", "agent_option", "-a", help="Hermes Agent 名称或 ID")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--manage", is_flag=True, help="打开 Hermes 管理 UI (/)")
@click.option("--chat", is_flag=True, help="打开统一 hosted chat (/chat)")
@click.option("--path", "ui_path", default=None, help="目标 UI 路径")
@click.option("--share", is_flag=True, help="创建可分享链接")
@click.option("--expires-seconds", default=None, type=str, help="链接有效期（秒）")
@click.option("--force-new", is_flag=True, help="强制新建链接（跳过复用）")
@click.option("--no-open", is_flag=True, help="仅打印 URL，不自动打开浏览器")
@click.option("--direct", is_flag=True, help="直接打开 endpoint/path（跳过短链接创建）")
@dry_run_option()
@cli_output_option()
def open_hermes(
    agent_ref: Optional[str],
    agent_option: Optional[str],
    region: str,
    manage: bool,
    chat: bool,
    ui_path: Optional[str],
    share: bool,
    expires_seconds: Optional[str],
    force_new: bool,
    no_open: bool,
    direct: bool,
    dry_run: bool,
    output_mode: str | None,
):
    """打开 Hermes 管理 UI，或使用 --chat 打开统一聊天页。"""
    _ = output_mode
    ctx = click.get_current_context(silent=True)
    parameter_source = ctx.get_parameter_source("region") if ctx is not None else None
    region_source = parameter_source.name.lower() if parameter_source is not None else ""
    if manage and chat:
        raise click.ClickException("--manage 与 --chat 不能同时使用")
    try:
        positional_agent = merge_agent_inputs(agent_option=agent_option, positional_agent=agent_ref)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    dry_run = effective_dry_run(dry_run)
    target_path = ui_path or ("/chat" if chat else "/")
    parsed_expires = (
        int(expires_seconds)
        if expires_seconds is not None and expires_seconds not in {"never", "forever"}
        else 0 if expires_seconds else None
    )
    if dry_run:
        _render_hermes_dry_run(
            "open",
            {
                "agent_ref": positional_agent,
                "path": target_path,
                "share": share,
                "expires_seconds": parsed_expires,
                "force_new": force_new,
                "no_open": no_open,
                "direct": direct,
            },
            hints=("dry-run 未解析远端 Agent，也未打开浏览器。",),
        )
        return
    verified_ref = _resolve_hermes_ref(positional_agent)
    detail = asyncio.run(_get_hermes_detail(region, verified_ref, include_api_key=False))
    positional_agent = str(detail.get("agent_id") or verified_ref)
    _open_dashboard(
        positional_agent=positional_agent,
        agent_option=None,
        region=region,
        region_source=region_source,
        ui_path=target_path,
        share=share,
        expires_seconds=parsed_expires,
        force_new=force_new,
        no_open=no_open,
        direct=direct,
    )


@hermes.command("exec", context_settings=CONTEXT_SETTINGS)
@click.argument("argv", nargs=-1, required=True)
@click.option("--agent", "agent_option", default=None, help="Hermes Agent 名称（显式指定）")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--endpoint", "-e", default=None, help="Agent Endpoint URL (覆盖自动获取)")
@click.option("--api-key", default=None, help="AgentEngine API Key (覆盖自动获取)")
@click.option("--session", "-s", default=None, help="Session ID")
@click.option("--insecure", "-k", is_flag=True, help="跳过 SSL 证书验证")
@dry_run_option()
@cli_output_option()
def exec_hermes(
    argv: tuple[str, ...],
    agent_option: Optional[str],
    region: str,
    endpoint: Optional[str],
    api_key: Optional[str],
    session: Optional[str],
    insecure: bool,
    dry_run: bool,
    output_mode: str | None,
):
    """透传受限 Hermes 只读运维子命令。"""
    _ = output_mode
    try:
        if agent_option is not None:
            agent_option = agent_option.strip()
            if not agent_option:
                raise click.ClickException("--agent 必须指定非空的 Hermes Agent 名称")
        positional_agent_ref, validated_argv = _split_terminal_agent_ref_and_argv(
            argv,
            validator=_passthrough_exec_argv,
        )
        if agent_option is not None and positional_agent_ref:
            raise click.ClickException(
                "--agent 不能与位置参数 Agent ID 同时使用: " f"{positional_agent_ref}"
            )
        agent_ref = agent_option if agent_option is not None else positional_agent_ref
        dry_run = effective_dry_run(dry_run)
        if dry_run:
            _render_hermes_dry_run(
                "exec",
                {
                    "agent_ref": agent_ref,
                    "endpoint": endpoint,
                    "mode": "exec",
                    "argv": validated_argv,
                    "session": session,
                    "insecure": insecure,
                },
                hints=("dry-run 未解析远端 Agent，也未建立 websocket。",),
            )
            return
        access = _resolve_hermes_access(
            agent_ref=agent_ref, region=region, endpoint=endpoint, api_key=api_key
        )
        exit_code = asyncio.run(
            run_hermes_terminal_session(
                endpoint=str(access["endpoint"]),
                api_key=access.get("api_key"),
                session_id=session,
                insecure=insecure,
                mode="exec",
                argv=validated_argv,
                exec_argv_validator=_passthrough_exec_argv,
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise SystemExit(130)
    except ValueError as e:
        raise click.ClickException(f"不允许的 Hermes 子命令: {e}") from e
    if exit_code:
        raise SystemExit(exit_code)


@hermes.command("pairing", context_settings=CONTEXT_SETTINGS)
@click.argument("argv", nargs=-1, required=True)
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--endpoint", "-e", default=None, help="Agent Endpoint URL (覆盖自动获取)")
@click.option("--api-key", default=None, help="AgentEngine API Key (覆盖自动获取)")
@click.option("--session", "-s", default=None, help="Session ID")
@click.option("--insecure", "-k", is_flag=True, help="跳过 SSL 证书验证")
@dry_run_option()
@cli_output_option()
def pairing_hermes(
    argv: tuple[str, ...],
    region: str,
    endpoint: Optional[str],
    api_key: Optional[str],
    session: Optional[str],
    insecure: bool,
    dry_run: bool,
    output_mode: str | None,
):
    """透传 Hermes pairing 审批子命令。

    WPS 协作配对码来自未授权用户私聊机器人时 Hermes 返回的 pairing code，
    审批示例：agentengine hermes pairing <agent> -- approve wpsxiezuo <code>
    """
    _ = output_mode
    try:
        agent_ref, validated_argv = _split_terminal_agent_ref_and_argv(
            argv,
            validator=validate_hermes_pairing_argv,
        )
        dry_run = effective_dry_run(dry_run)
        if dry_run:
            _render_hermes_dry_run(
                "pairing",
                {
                    "agent_ref": agent_ref,
                    "endpoint": endpoint,
                    "mode": "pairing",
                    "argv": validated_argv,
                    "session": session,
                    "insecure": insecure,
                },
                hints=("dry-run 未解析远端 Agent，也未建立 websocket。",),
            )
            return
        access = _resolve_hermes_access(
            agent_ref=agent_ref, region=region, endpoint=endpoint, api_key=api_key
        )
        exit_code = asyncio.run(
            run_hermes_terminal_session(
                endpoint=str(access["endpoint"]),
                api_key=access.get("api_key"),
                session_id=session,
                insecure=insecure,
                mode="pairing",
                argv=validated_argv,
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise SystemExit(130)
    except ValueError as e:
        raise click.ClickException(f"不允许的 Hermes pairing 子命令: {e}") from e
    if exit_code:
        raise SystemExit(exit_code)


@hermes.command("connect", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--endpoint", "-e", default=None, help="Agent Endpoint URL (覆盖自动获取)")
@click.option("--api-key", default=None, help="AgentEngine API Key (覆盖自动获取)")
@click.option("--session", "-s", default=None, help="Session ID")
@click.option("--insecure", "-k", is_flag=True, help="跳过 SSL 证书验证")
@dry_run_option()
@cli_output_option()
def connect_hermes(
    agent_ref: Optional[str],
    region: str,
    endpoint: Optional[str],
    api_key: Optional[str],
    session: Optional[str],
    insecure: bool,
    dry_run: bool,
    output_mode: str | None,
):
    """进入远端 Hermes gateway setup 向导，执行扫码连接。"""
    _ = output_mode
    dry_run = effective_dry_run(dry_run)
    if dry_run:
        _render_hermes_dry_run(
            "connect",
            {
                "agent_ref": agent_ref,
                "endpoint": endpoint,
                "mode": "connect",
                "session": session,
                "insecure": insecure,
            },
            hints=("dry-run 未解析远端 Agent，也未建立 websocket。",),
        )
        return

    try:
        access = _resolve_hermes_access(
            agent_ref=agent_ref, region=region, endpoint=endpoint, api_key=api_key
        )
        exit_code = asyncio.run(
            run_hermes_terminal_session(
                endpoint=str(access["endpoint"]),
                api_key=access.get("api_key"),
                session_id=session,
                insecure=insecure,
                mode="connect",
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise SystemExit(130)
    if exit_code:
        raise SystemExit(exit_code)


def _delete_impl(agent_refs: tuple[str, ...], region: str, assume_yes: bool, dry_run: bool):
    dry_run = effective_dry_run(dry_run)
    if not confirm_destructive(
        assume_yes=assume_yes,
        dry_run=dry_run,
        prompt=f"确定要删除这 {len(agent_refs)} 个 Hermes Agent 吗?",
    ):
        return

    async def _delete():
        deleted = []
        failed = []
        async with AgentEngineClient(region=region, dry_run=dry_run) as client:
            for agent_ref in agent_refs:
                detail = await _get_hermes_detail_with_client(client, agent_ref)
                agent_id = str(detail.get("agent_id") or "").strip()
                if not agent_id:
                    failed.append(agent_ref)
                    continue
                ok = await client.delete_agent(agent_id)
                if ok:
                    deleted.append(agent_id)
                    clear_state(Path(".").resolve(), key=agent_id)
                else:
                    failed.append(agent_ref)
        if failed:
            raise remote_error(f"以下 Hermes 删除失败: {', '.join(failed)}")
        return {"targets": list(agent_refs), "deleted": deleted, "failed": failed}

    result = run_async_with_dry_run(
        _delete(), dry_run=dry_run, dry_run_resource="hermes", dry_run_action="delete"
    )
    if result is not None:
        deleted_text = ", ".join(result["deleted"]) or "-"
        failed_text = ", ".join(result["failed"]) or "-"
        render_descriptor_status(
            HERMES_RESOURCE,
            title="Hermes 删除结果",
            subtitle=", ".join(result["targets"]) if result["targets"] else "-",
            fields=[
                ("目标数量", str(len(result["targets"])), None),
                ("已删除", deleted_text, "ok" if result["deleted"] else "muted"),
                ("失败", failed_text, "err" if result["failed"] else "muted"),
            ],
            next_steps=(
                "agentengine hermes list",
                "agentengine hermes deploy",
            ),
            action="delete",
            item=result,
        )


@hermes.command("delete", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_refs", nargs=-1, required=True)
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@confirm_options()
@dry_run_option()
@cli_output_option()
def delete(
    agent_refs: tuple[str, ...],
    region: str,
    assume_yes: bool,
    dry_run: bool,
    output_mode: str | None,
):
    """删除 Hermes Agent。"""
    _ = output_mode
    _delete_impl(agent_refs=agent_refs, region=region, assume_yes=assume_yes, dry_run=dry_run)


@hermes.command("destroy", context_settings=CONTEXT_SETTINGS, hidden=True)
@click.argument("agent_refs", nargs=-1, required=True)
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@confirm_options()
@dry_run_option()
@cli_output_option()
def destroy(
    agent_refs: tuple[str, ...],
    region: str,
    assume_yes: bool,
    dry_run: bool,
    output_mode: str | None,
):
    """删除 Hermes Agent。"""
    _ = output_mode
    _delete_impl(agent_refs=agent_refs, region=region, assume_yes=assume_yes, dry_run=dry_run)
