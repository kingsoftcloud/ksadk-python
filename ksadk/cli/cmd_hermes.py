"""agentengine hermes - Hermes resource management."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Optional

import click

from ksadk.api import AgentEngineClient
from ksadk.cli.agent_ref import merge_agent_inputs, resolve_agent_ref
from ksadk.cli.cmd_dashboard import _open_dashboard
from ksadk.cli.dry_run import dry_run_option, effective_dry_run, run_async_with_dry_run
from ksadk.cli.error_utils import remote_error, resolution_error
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
    render_descriptor_list,
    render_descriptor_status,
)
from ksadk.cli.ui import (
    emit_json,
    is_json_output,
    output_option as cli_output_option,
    print_info,
    print_kv,
    print_rule,
    print_success,
    print_title,
    print_warn,
)
from ksadk.deployment.state import clear_state, load_state, save_state
from ksadk.hermes_terminal import (
    run_hermes_terminal_session,
    validate_hermes_exec_argv,
    validate_hermes_pairing_argv,
)


DEFAULT_HERMES_IMAGE = "hub.kce.ksyun.com/agentengine-public/hermes-agent:v2026.4.13-ks8"
DEFAULT_HERMES_CONTEXT_LENGTHS = (
    ("glm-5.1", "200000"),
)
DEFAULT_HERMES_FALLBACK_MODELS = (
    ("glm-5.1", "kimi-k2.5"),
)
KSPMAS_PUBLIC_BASES = (
    "http://kspmas.ksyun.com",
    "https://kspmas.ksyun.com",
)
KSPMAS_INTERNAL_BASE = "http://kspmas-internal.sdns.ksyun.com"

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
        extra=("exec", "pairing"),
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
            "agentengine hermes open <agent> --chat",
            "agentengine hermes exec <agent> -- status",
            "agentengine hermes pairing <agent> -- list",
        ),
    ),
    examples=(
        "agentengine hermes deploy --name demo-hermes",
        "agentengine hermes list",
        "agentengine hermes status ar-xxxx",
        "agentengine hermes open ar-xxxx --manage",
        "agentengine hermes open ar-xxxx --chat",
        "agentengine hermes exec ar-xxxx -- status",
        "agentengine hermes pairing ar-xxxx -- list",
        "agentengine hermes delete ar-xxxx",
    ),
    missing_ref_message="未找到 Hermes Agent，请指定 Agent（--agent 或位置参数）",
    resolution_commands=("agentengine hermes list",),
)


@click.group("hermes", context_settings=CONTEXT_SETTINGS)
def hermes():
    """Hermes Agent 资源管理。"""


def _load_dotenv_into_env(project_dir: Path) -> None:
    env_path = project_dir / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import dotenv_values
    except ImportError:
        return
    for key, value in dotenv_values(env_path).items():
        if key and value is not None and os.getenv(key) is None:
            os.environ[key] = str(value)


def _env_value(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return ""


async def _fetch_hermes_bootstrap_config(region: str) -> dict[str, Any] | None:
    """从服务端获取 Hermes 客户端启动配置。失败时返回 None。"""
    from ksadk.version import VERSION as CLI_VERSION

    try:
        async with AgentEngineClient(region=region) as client:
            return await client.get_client_bootstrap_config(
                product="hermes",
                framework="hermes",
                region=region,
                client_type="cli",
                client_version=CLI_VERSION,
                locale=_env_value("LANG", "LC_ALL"),
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


def _default_fallback_model_for_model(model: str | None, base_url: str | None) -> str:
    normalized_model = str(model or "").strip().lower()
    normalized_base_url = str(base_url or "").strip().lower()
    for model_fragment, fallback_model in DEFAULT_HERMES_FALLBACK_MODELS:
        if model_fragment in normalized_model:
            return fallback_model
    if "kspmas" in normalized_base_url:
        return "kimi-k2.5"
    return ""


def _build_hermes_env_vars(
    *,
    model_base_url: str | None = None,
    model_api_key: str | None = None,
    default_model: str | None = None,
) -> list[dict[str, Any]]:
    resolved_model_base_url = _normalize_hermes_runtime_base_url(
        model_base_url or _env_value("OPENAI_BASE_URL")
    )
    resolved_default_model = default_model or _env_value("OPENAI_MODEL_NAME")
    context_length = (
        _env_value("HERMES_CONTEXT_LENGTH", "OPENAI_CONTEXT_LENGTH", "MODEL_CONTEXT_LENGTH")
        or _default_context_length_for_model(resolved_default_model)
    )
    fallback_model = (
        _env_value("HERMES_FALLBACK_MODEL", "OPENAI_FALLBACK_MODEL_NAME")
        or _default_fallback_model_for_model(resolved_default_model, resolved_model_base_url)
    )
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
    }
    if context_length:
        raw["HERMES_CONTEXT_LENGTH"] = context_length
    if fallback_model:
        raw["HERMES_FALLBACK_PROVIDER"] = _env_value("HERMES_FALLBACK_PROVIDER") or "custom"
        raw["HERMES_FALLBACK_MODEL"] = fallback_model
        raw["HERMES_FALLBACK_BASE_URL"] = _env_value("HERMES_FALLBACK_BASE_URL") or resolved_model_base_url
    api_server_key = _env_value("API_SERVER_KEY", "HERMES_API_SERVER_KEY")
    if api_server_key:
        raw["API_SERVER_KEY"] = api_server_key
    return [
        {"Key": key, "Value": str(value), "IsSensitive": any(token in key for token in ("KEY", "TOKEN", "SECRET"))}
        for key, value in raw.items()
        if value is not None
    ]


def _normalize_hermes_runtime_base_url(base_url: str | None) -> str:
    normalized = str(base_url or "").strip()
    if not normalized:
        return normalized
    for prefix in KSPMAS_PUBLIC_BASES:
        if normalized.startswith(prefix):
            return normalized.replace(prefix, KSPMAS_INTERNAL_BASE, 1)
    return normalized


def _flatten_agent_detail(agent: dict[str, Any]) -> dict[str, Any]:
    basic = agent.get("basic") if isinstance(agent.get("basic"), dict) else {}
    deployment = agent.get("deployment") if isinstance(agent.get("deployment"), dict) else {}
    quick = agent.get("quick_access") if isinstance(agent.get("quick_access"), dict) else {}
    return {
        "agent_id": basic.get("agent_id") or agent.get("agent_id"),
        "name": basic.get("name") or agent.get("name"),
        "status": basic.get("status") or agent.get("status") or "UNKNOWN",
        "framework": deployment.get("framework") or basic.get("framework") or agent.get("framework"),
        "region": basic.get("region") or agent.get("region"),
        "endpoint": quick.get("public_endpoint") or quick.get("private_endpoint") or agent.get("endpoint"),
        "api_key": quick.get("api_key") or agent.get("api_key"),
        "artifact_path": deployment.get("artifact_path") or agent.get("artifact_path"),
    }


def _resolve_hermes_ref(agent_ref: str | None) -> str:
    resolved = resolve_agent_ref(agent_ref, cwd=Path(".").resolve(), include_state=True, include_project_config=True)
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
        raise resolution_error(f"目标 Agent 不是 Hermes: {agent_ref}", hints=["agentengine hermes list"])
    return detail


async def _get_hermes_detail(region: str, agent_ref: str, *, include_api_key: bool = False) -> dict[str, Any]:
    async with AgentEngineClient(region=region) as client:
        return await _get_hermes_detail_with_client(client, agent_ref, include_api_key=include_api_key)


async def _poll_hermes_order_access(
    client: AgentEngineClient,
    *,
    agent_name: str,
    attempts: int = 12,
    interval_seconds: int = 5,
) -> dict[str, Any]:
    for attempt in range(max(1, attempts)):
        if attempt > 0:
            await asyncio.sleep(interval_seconds)
        try:
            detail = await _get_hermes_detail_with_client(client, agent_name, include_api_key=True)
        except Exception:
            continue
        if detail.get("agent_id"):
            return detail
    return {}


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
        raise resolution_error("目标 Hermes Agent 未返回 Endpoint", hints=["agentengine hermes status <agent>"])
    return {
        "endpoint": endpoint_value,
        "api_key": api_key or detail.get("api_key"),
        "agent_id": detail.get("agent_id"),
        "name": detail.get("name"),
    }


def _render_hermes_dry_run(action: str, request: dict[str, Any], hints: tuple[str, ...] = ()) -> None:
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
    dry_run: bool,
    output_mode: str | None,
):
    """部署 Hermes runtime 到云端。"""
    _ = output_mode
    dry_run = effective_dry_run(dry_run)
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
    dry_run: bool,
) -> None:
    project_dir = Path(".").resolve()
    _load_dotenv_into_env(project_dir)
    state = load_state(project_dir)
    existing_agent_id = None
    if str(state.get("type") or state.get("framework") or "").strip().lower() == "hermes":
        existing_agent_id = str(state.get("agent_id") or "").strip() or None
    agent_name = name or state.get("name") or project_dir.name.replace("-", "_")
    image_ref = image or _env_value("HERMES_IMAGE", "HERMES_DOCKER_IMAGE")
    if not image_ref:
        bootstrap_cfg = await _fetch_hermes_bootstrap_config(region)
        image_ref = _extract_hermes_bootstrap_image(bootstrap_cfg)
        if image_ref:
            print_info(f"未指定镜像，使用服务端默认镜像: {image_ref}")
    image_ref = image_ref or DEFAULT_HERMES_IMAGE
    env_vars = _build_hermes_env_vars(
        model_base_url=model_base_url,
        model_api_key=model_api_key,
        default_model=default_model,
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
        "env_vars": env_vars,
        "ui_config": {"profile": "hermes", "path": "/", "url": None},
    }

    print_title("Hermes 云端部署", f"region: {region}")
    print_kv("名称", agent_name)
    print_kv("镜像", image_ref)

    async with AgentEngineClient(region=region, dry_run=dry_run) as client:
        if existing_agent_id:
            res = await client.update_agent(existing_agent_id, payload)
            if res is None:
                res = {}
            res.setdefault("agent_id", existing_agent_id)
            res.setdefault("endpoint", state.get("endpoint"))
            res.setdefault("api_key", state.get("api_key"))
        else:
            res = await client.create_agent(payload)
            if isinstance(res, dict) and res.get("order_id") and not res.get("agent_id"):
                print_info(f"订单已创建: {res.get('order_id')}，等待 Hermes 实例创建...")
                latest = await _poll_hermes_order_access(client, agent_name=agent_name)
                if latest:
                    res = {
                        **res,
                        "agent_id": latest.get("agent_id"),
                        "name": latest.get("name") or agent_name,
                        "endpoint": latest.get("endpoint"),
                        "api_key": latest.get("api_key"),
                    }
                else:
                    print_warn("实例仍在创建中，稍后使用 `agentengine hermes status` 查看")
    if dry_run:
        return

    agent_id = res.get("agent_id")
    endpoint = res.get("endpoint")
    api_key = res.get("api_key")
    save_state(
        project_dir,
        {
            "type": "hermes",
            "framework": "hermes",
            "agent_id": agent_id,
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
        emit_json(
            build_result_envelope(
                resource="hermes",
                action="deploy",
                result={
                    "id": str(agent_id or ""),
                    "agent_id": str(agent_id or ""),
                    "name": str(res.get("name") or agent_name),
                    "status": str(res.get("status") or res.get("phase") or "SUBMITTED"),
                    "framework": "hermes",
                    "region": region,
                    "endpoint": str(endpoint or ""),
                    "image": image_ref,
                    "ui_profile": "hermes",
                    "ui_path": "/",
                },
                hints=list(HERMES_RESOURCE.status_schema.next_steps),
            )
        )
        return
    print_success("Hermes 已提交部署")
    print_kv("Agent ID", str(agent_id or "(创建中)"))
    if endpoint:
        print_kv("Endpoint", str(endpoint), value_style="#58a6ff")
    print_info("已保存状态到 .agentengine.state")


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
            resp = await client.list_agents(region=region, framework="hermes", page=page, page_size=size)
        agents = resp.get("agents", []) or []
        rows = []
        items = []
        for agent in agents:
            detail = _flatten_agent_detail(agent)
            status = str(detail.get("status") or "UNKNOWN").upper()
            row = (
                str(detail.get("agent_id") or "-"),
                str(detail.get("name") or "-"),
                status,
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

    run_async_with_dry_run(_list(), dry_run=dry_run, dry_run_resource="hermes", dry_run_action="list")


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
        render_descriptor_status(
            HERMES_RESOURCE,
            subtitle=str(detail.get("name") or resolved),
            fields=[
                ("ID", str(detail.get("agent_id") or "-"), "#58a6ff"),
                ("状态", str(detail.get("status") or "UNKNOWN"), None),
                ("框架", str(detail.get("framework") or "-"), None),
                ("区域", str(detail.get("region") or region), None),
                ("Endpoint", str(detail.get("endpoint") or "-"), "#58a6ff"),
                ("镜像", str(detail.get("artifact_path") or "-"), None),
            ],
            item={
                "id": str(detail.get("agent_id") or "-"),
                "name": str(detail.get("name") or resolved),
                "status": str(detail.get("status") or "UNKNOWN"),
                "framework": str(detail.get("framework") or "-"),
                "region": str(detail.get("region") or region),
                "endpoint": str(detail.get("endpoint") or "-"),
                "image": str(detail.get("artifact_path") or "-"),
            },
        )

    run_async_with_dry_run(_status(), dry_run=dry_run, dry_run_resource="hermes", dry_run_action="status")


@hermes.command("open", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@click.option("--agent", "--agent-id", "agent_option", "-a", help="Hermes Agent 名称或 ID")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--manage", is_flag=True, help="打开 Hermes 管理 UI (/)")
@click.option("--chat", is_flag=True, help="打开统一 hosted chat (/chat)")
@click.option("--path", "ui_path", default=None, help="目标 UI 路径")
@click.option("--share", is_flag=True, help="创建可分享链接")
@click.option("--expires-seconds", default=None, type=str, help="链接有效期（秒）")
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
    no_open: bool,
    direct: bool,
    dry_run: bool,
    output_mode: str | None,
):
    """打开 Hermes 管理 UI，或使用 --chat 打开统一聊天页。"""
    _ = output_mode
    if manage and chat:
        raise click.ClickException("--manage 与 --chat 不能同时使用")
    try:
        positional_agent = merge_agent_inputs(agent_option=agent_option, positional_agent=agent_ref)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    dry_run = effective_dry_run(dry_run)
    target_path = ui_path or ("/chat" if chat else "/")
    parsed_expires = int(expires_seconds) if expires_seconds is not None and expires_seconds not in {"never", "forever"} else 0 if expires_seconds else None
    if dry_run:
        _render_hermes_dry_run(
            "open",
            {
                "agent_ref": positional_agent,
                "path": target_path,
                "share": share,
                "expires_seconds": parsed_expires,
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
        ui_path=target_path,
        share=share,
        expires_seconds=parsed_expires,
        no_open=no_open,
        direct=direct,
    )


@hermes.command("exec", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@click.argument("argv", nargs=-1, required=True)
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--endpoint", "-e", default=None, help="Agent Endpoint URL (覆盖自动获取)")
@click.option("--api-key", default=None, help="AgentEngine API Key (覆盖自动获取)")
@click.option("--session", "-s", default=None, help="Session ID")
@click.option("--insecure", "-k", is_flag=True, help="跳过 SSL 证书验证")
@dry_run_option()
@cli_output_option()
def exec_hermes(
    agent_ref: Optional[str],
    argv: tuple[str, ...],
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
        validated_argv = validate_hermes_exec_argv(argv)
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
        access = _resolve_hermes_access(agent_ref=agent_ref, region=region, endpoint=endpoint, api_key=api_key)
        exit_code = asyncio.run(
            run_hermes_terminal_session(
                endpoint=str(access["endpoint"]),
                api_key=access.get("api_key"),
                session_id=session,
                insecure=insecure,
                mode="exec",
                argv=validated_argv,
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise SystemExit(130)
    except ValueError as e:
        raise click.ClickException(f"不允许的 Hermes 子命令: {e}") from e
    if exit_code:
        raise SystemExit(exit_code)


@hermes.command("pairing", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_ref", required=False)
@click.argument("argv", nargs=-1, required=True)
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--endpoint", "-e", default=None, help="Agent Endpoint URL (覆盖自动获取)")
@click.option("--api-key", default=None, help="AgentEngine API Key (覆盖自动获取)")
@click.option("--session", "-s", default=None, help="Session ID")
@click.option("--insecure", "-k", is_flag=True, help="跳过 SSL 证书验证")
@dry_run_option()
@cli_output_option()
def pairing_hermes(
    agent_ref: Optional[str],
    argv: tuple[str, ...],
    region: str,
    endpoint: Optional[str],
    api_key: Optional[str],
    session: Optional[str],
    insecure: bool,
    dry_run: bool,
    output_mode: str | None,
):
    """透传 Hermes pairing 审批子命令。"""
    _ = output_mode
    try:
        validated_argv = validate_hermes_pairing_argv(argv)
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
        access = _resolve_hermes_access(agent_ref=agent_ref, region=region, endpoint=endpoint, api_key=api_key)
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

    result = run_async_with_dry_run(_delete(), dry_run=dry_run, dry_run_resource="hermes", dry_run_action="delete")
    if result is not None:
        render_descriptor_status(
            HERMES_RESOURCE,
            title="Hermes 删除结果",
            subtitle=", ".join(result["targets"]) if result["targets"] else "-",
            fields=[
                ("目标数量", str(len(result["targets"])), None),
                ("已删除", ", ".join(result["deleted"]) or "-", None),
                ("失败", ", ".join(result["failed"]) or "-", None),
            ],
            action="delete",
            item=result,
        )


@hermes.command("delete", context_settings=CONTEXT_SETTINGS)
@click.argument("agent_refs", nargs=-1, required=True)
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@confirm_options()
@dry_run_option()
@cli_output_option()
def delete(agent_refs: tuple[str, ...], region: str, assume_yes: bool, dry_run: bool, output_mode: str | None):
    """删除 Hermes Agent。"""
    _ = output_mode
    _delete_impl(agent_refs=agent_refs, region=region, assume_yes=assume_yes, dry_run=dry_run)


@hermes.command("destroy", context_settings=CONTEXT_SETTINGS, hidden=True)
@click.argument("agent_refs", nargs=-1, required=True)
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@confirm_options()
@dry_run_option()
@cli_output_option()
def destroy(agent_refs: tuple[str, ...], region: str, assume_yes: bool, dry_run: bool, output_mode: str | None):
    """删除 Hermes Agent。"""
    _ = output_mode
    _delete_impl(agent_refs=agent_refs, region=region, assume_yes=assume_yes, dry_run=dry_run)
