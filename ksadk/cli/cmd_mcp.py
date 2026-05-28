"""agentengine mcp - MCP 资源管理。"""

import os
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime
from pathlib import Path

import click

from ksadk.api.client import DryRunExit
from ksadk.cli.agent_ref import resolve_mcp_ref
from ksadk.cli.dry_run import dry_run_option, run_async_with_dry_run, effective_dry_run
from ksadk.cli.error_utils import abort_with_cli_error, remote_error, resolution_error, usage_error, validation_error
from ksadk.cli.network_options import build_network_payload, network_cli_kwargs, network_options
from ksadk.cli.resource_common import (
    CONTEXT_SETTINGS,
    ResourceActionSet,
    ResourceDescriptor,
    ResourceListSchema,
    ResourceStatusSchema,
    build_resource_group_help,
    confirm_destructive,
    confirm_options,
    pagination_options,
    print_next_action_hint,
    render_descriptor_list,
    render_descriptor_status,
    region_option,
)
from ksadk.cli.ui import (
    capture_standard_output,
    get_console,
    is_json_output,
    output_option as cli_output_option,
    print_info,
    print_kv,
    print_rule,
    print_success,
    print_title,
    print_warn,
    status_rich_style,
)
from ksadk.cli.workflow_common import (
    build_workflow_local_plan,
    clear_build_metadata,
    emit_no_cache_hint,
    load_cached_artifact_reference,
    plan_artifact_build,
    print_workflow_header,
    render_workflow_dry_run,
    render_workflow_result,
    resolve_artifact_build_plan,
)

console = get_console()

MCP_RESOURCE = ResourceDescriptor(
    name="MCP",
    summary="MCP 资源管理。",
    resource_key="mcp",
    actions=ResourceActionSet(
        list="agentengine mcp list",
        status="agentengine mcp status [mcp_ref]",
        delete="agentengine mcp delete [mcp_ref...]",
        deploy="agentengine mcp deploy",
        extra=("agentengine mcp build",),
    ),
    list_schema=ResourceListSchema(
        title="MCP 列表",
        noun="MCP",
        columns=(
            {"header": "ID", "key": "id", "style": "#58a6ff", "no_wrap": True},
            {"header": "名称", "key": "name", "style": "white"},
            {"header": "状态", "key": "status", "no_wrap": True, "justify": "center"},
            {"header": "MCP URL", "key": "mcp_url", "style": "#8b949e", "overflow": "ellipsis"},
        ),
        empty_message="没有找到已部署的 MCP",
        summary_lines=("使用 `agentengine mcp status <mcp_ref>` 查看详情。",),
    ),
    status_schema=ResourceStatusSchema(
        title="MCP 状态",
        next_steps=("agentengine mcp list",),
    ),
    examples=(
        "agentengine mcp deploy .",
        "agentengine mcp list",
        "KSYUN_REGION=cn-beijing-6 agentengine mcp status <id>",
    ),
    missing_ref_message="请指定 MCP ID/名称，或在 MCP 项目目录下运行",
    resolution_commands=("agentengine mcp list",),
    list_action_help="列出已部署的 MCP",
    status_action_help="查看单个 MCP 状态",
    delete_action_help="删除一个或多个 MCP",
    deploy_action_help="部署 MCP 到云端",
    extra_action_help=(("build", "构建 MCP 制品"),),
)


def _abort_mcp_error(
    err: Exception,
    *,
    context: str | None = None,
    argv: list[str] | None = None,
    show_help: bool = False,
) -> None:
    abort_with_cli_error(err, context=context, argv=argv, show_help=show_help)


@click.group("mcp", context_settings=CONTEXT_SETTINGS, help=build_resource_group_help(MCP_RESOURCE))
def mcp():
    pass


@mcp.command("build", context_settings=CONTEXT_SETTINGS)
@click.argument("mcp_dir", default=".", type=click.Path(exists=True))
@click.option(
    "--artifact-type",
    type=click.Choice(["Code", "Container"], case_sensitive=True),
    default="Code",
    help="构建模式: Code-代码包 (默认) 或 Container-镜像模式",
)
@click.option("--push", is_flag=True, help="构建后上传/推送制品")
@click.option("--tag", help="镜像标签 (Container 模式)")
@click.option("--registry", help="镜像仓库地址 (Container 模式)")
@click.option(
    "--region", "-r",
    default="cn-beijing-6",
    envvar="KSYUN_REGION",
    help="构建使用的区域 (Code 模式用于 KS3，Container 模式用于默认镜像仓库推断)",
)
@click.option(
    "--ks3-bucket",
    help="KS3 存储桶名称 (Code 模式，默认: agentengine-{account_id}-{region})"
)
@click.option(
    "--no-cache",
    is_flag=True,
    default=False,
    help="强制重新构建，不使用缓存 (Code/Container 模式均适用)",
)
@cli_output_option()
def build(
    mcp_dir: str,
    artifact_type: str,
    push: bool,
    tag: str | None,
    registry: str | None,
    region: str,
    ks3_bucket: str | None,
    no_cache: bool,
    output_mode: str | None,
):
    """构建 MCP Server 制品。"""
    _ = output_mode
    try:
        result = run_async_with_dry_run(
            _build_mcp_async(
                mcp_dir=mcp_dir,
                artifact_type=artifact_type,
                push=push,
                region=region,
                ks3_bucket=ks3_bucket,
                tag=tag,
                registry=registry,
                no_cache=no_cache,
            ),
            dry_run=False,
        )
    except Exception as e:
        _abort_mcp_error(e, context="构建失败", argv=["mcp", "build"])
        return
    if result is not None and is_json_output():
        render_workflow_result(action="build", result=result)


@mcp.command("deploy", context_settings=CONTEXT_SETTINGS)
@click.argument("mcp_dir", default=".", type=click.Path(exists=True))
@click.option(
    "--name", "-n",
    help="MCP Server 名称 (默认: 目录名)"
)
@click.option(
    "--region", "-r",
    default="cn-beijing-6",
    envvar="KSYUN_REGION",
    help="部署区域 (default: cn-beijing-6)"
)
@click.option(
    "--ks3-bucket",
    help="KS3 存储桶名称 (默认: agentengine-{region})"
)
@click.option(
    "--enable-auth",
    is_flag=True,
    default=False,
    help="启用 API Key 保护 (可选)"
)
@dry_run_option("仅显示请求内容，不实际部署")
@click.option(
    "--artifact-type",
    type=click.Choice(["Code", "Container"], case_sensitive=True),
    default="Code",
    help="部署模式: Code-代码包 (默认) 或 Container-镜像模式",
)
@click.option(
    "--no-cache",
    is_flag=True,
    default=False,
    help="强制重新构建，不使用缓存 (Code/Container 模式均适用)",
)
@network_options
@cli_output_option()
def deploy(
    mcp_dir: str,
    name: str,
    region: str,
    ks3_bucket: str,
    enable_auth: bool,
    dry_run: bool,
    artifact_type: str,
    no_cache: bool,
    enable_public_access: bool | None,
    enable_vpc_access: bool,
    vpc_id: str | None,
    subnet_id: str | None,
    security_group_id: str | None,
    availability_zone: str | None,
    output_mode: str | None,
):
    """部署 MCP Server 到云端
    
    \b
    MCP_DIR: MCP 项目目录 (默认: 当前目录)
    
    \b
    示例:
        # 1) 默认部署 (Code 模式)
        agentengine mcp deploy .
        # 2) 显式指定部署参数
        agentengine mcp deploy ./my-mcp --name my-tools --artifact-type Container
        # 3) 显式指定区域
        KSYUN_REGION=cn-beijing-6 agentengine mcp deploy . --dry-run
    
    \b
    部署后的 endpoint 兼容标准 MCP 协议，可以被:
        - LangGraph/LangChain (via langchain-mcp-adapters)
        - Google ADK (via MCPToolset)
        - Cursor / Claude Code
        - Dify 等外部平台
    """
    _ = output_mode
    dry_run = effective_dry_run(dry_run)
    dry_run_context: dict[str, object] = {}
    try:
        result = run_async_with_dry_run(
            _deploy_mcp_async(
                mcp_dir=mcp_dir,
                name=name,
                region=region,
                ks3_bucket=ks3_bucket,
                enable_auth=enable_auth,
                dry_run=dry_run,
                artifact_type=artifact_type,
                no_cache=no_cache,
                **network_cli_kwargs(
                    enable_public_access=enable_public_access,
                    enable_vpc_access=enable_vpc_access,
                    vpc_id=vpc_id,
                    subnet_id=subnet_id,
                    security_group_id=security_group_id,
                    availability_zone=availability_zone,
                ),
                dry_run_context=dry_run_context,
            ),
            dry_run=dry_run,
            on_dry_run=(
                lambda exc: render_workflow_dry_run(
                    action="deploy",
                    request=dict(exc.payload or {}),
                    plan=dict(dry_run_context.get("plan") or {}) or None,
                )
            ),
        )
    except Exception as e:
        _abort_mcp_error(e, context="部署失败", argv=["mcp", "deploy"])
        return
    if result is not None and is_json_output():
        render_workflow_result(action="deploy", result=result)


def _load_mcp_config(mcp_path: Path) -> dict:
    """加载 MCP 项目配置文件。"""
    import yaml

    config_path = mcp_path / "agentengine.yaml"
    if not config_path.exists():
        config_path = mcp_path / "ksadk.yaml"

    if not config_path.exists():
        return {}

    with open(config_path, encoding="utf-8-sig") as f:
        return yaml.safe_load(f) or {}


def _normalize_mcp_name(name: str | None, mcp_path: Path, config: dict | None = None) -> str:
    raw_name = (name or (config or {}).get("name") or mcp_path.name).strip()
    return raw_name.replace("-", "_").replace(".", "_")


def _normalize_artifact_type(artifact_type: str | None) -> str:
    return "Container" if str(artifact_type or "").strip().lower() == "container" else "Code"


def _normalize_upload_region(region: str) -> str:
    return "cn-beijing-6" if str(region or "").strip().lower() == "pre-online" else region


def _default_container_tag(config: dict, tag: str | None) -> str:
    return (
        str(tag or "").strip()
        or str(config.get("version") or "").strip()
        or str((config.get("image") or {}).get("tag") or "").strip()
        or "latest"
    )


def _default_container_registry(config: dict, registry: str | None) -> str:
    return (
        str(registry or "").strip().rstrip("/")
        or str(os.getenv("KCR_REGISTRY") or "").strip().rstrip("/")
        or str((config.get("image") or {}).get("registry") or "").strip().rstrip("/")
        or "ghcr.io/kingsoftcloud/agentengine"
    )


def _parse_ks3_bucket(artifact_path: str | None) -> str | None:
    normalized = str(artifact_path or "").strip()
    if not normalized.startswith("ks3://"):
        return None
    bucket_and_key = normalized[6:]
    if "/" not in bucket_and_key:
        return bucket_and_key or None
    bucket, _ = bucket_and_key.split("/", 1)
    return bucket or None


def _predict_mcp_artifact_reference(
    *,
    artifact_type: str,
    mcp_name: str,
    region: str,
    ks3_bucket: str | None,
    registry: str | None,
    account_id: str | None,
) -> str:
    if artifact_type == "Code":
        bucket = str(ks3_bucket or "").strip()
        if not bucket:
            normalized_region = _normalize_upload_region(region)
            if account_id and normalized_region:
                bucket = f"agentengine-{account_id}-{normalized_region}"
        if not bucket:
            bucket = "<ks3-bucket>"
        return f"ks3://{bucket}/mcps/{mcp_name}/code_<dry-run>.zip"

    normalized_registry = str(registry or "").strip().rstrip("/") or "<registry>"
    return f"{normalized_registry}/{mcp_name}:dry-run"


def _print_mcp_detection(detection_result) -> None:
    print_success("检测到 FastMCP 项目")
    print_kv("入口", detection_result.entry_point)
    print_kv("MCP 变量", detection_result.mcp_variable)
    if detection_result.tools:
        print_kv("工具", ", ".join(detection_result.tools))


def _print_mcp_build_summary(
    *,
    artifact_type: str,
    build_result,
    artifact_reference: str,
    push: bool,
) -> None:
    print_rule(f"构建摘要 ({artifact_type} 模式)")
    if build_result.artifact_path:
        print_kv("zip 文件", str(build_result.artifact_path))
        print_kv("大小", f"{build_result.artifact_size_mb:.2f} MB")
    if build_result.metadata.get("image"):
        print_kv("镜像", str(build_result.metadata["image"]))
    if artifact_type == "Code" and push and artifact_reference:
        print_kv("KS3 路径", artifact_reference, value_style="#58a6ff")
    if artifact_type == "Container" and push and artifact_reference:
        print_kv("镜像仓库", artifact_reference, value_style="#58a6ff")
    if build_result.metadata.get("mcp_variable"):
        print_kv("MCP 变量", str(build_result.metadata["mcp_variable"]))
    if build_result.metadata.get("tools"):
        print_kv("工具", ", ".join(build_result.metadata["tools"]))


def _persist_mcp_build_metadata(mcp_path: Path, build_result, *, artifact_type: str, artifact_reference: str) -> None:
    metadata_file = mcp_path / ".agentengine" / "build-metadata.json"
    if is_dataclass(build_result):
        payload = asdict(build_result)
    else:
        payload = {
            "success": bool(getattr(build_result, "success", False)),
            "artifact_path": getattr(build_result, "artifact_path", None),
            "artifact_size": int(getattr(build_result, "artifact_size", 0) or 0),
            "metadata": dict(getattr(build_result, "metadata", {}) or {}),
            "error_message": getattr(build_result, "error_message", None),
        }
    payload["metadata"] = dict(payload.get("metadata") or {})

    if artifact_type == "Code":
        payload["metadata"]["ks3_path"] = artifact_reference
    else:
        payload["metadata"]["image"] = artifact_reference
        payload["image"] = artifact_reference

    def _serialize(obj):
        if isinstance(obj, Path):
            return str(obj)
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

    metadata_file.parent.mkdir(parents=True, exist_ok=True)
    import json

    with open(metadata_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=_serialize)


async def _build_code_artifact(
    *,
    mcp_path: Path,
    mcp_name: str,
    region: str,
    ks3_bucket: str | None,
    no_cache: bool,
    push: bool,
):
    from ksadk.builders.ks3_uploader import KS3Uploader
    from ksadk.builders.mcp_builder import MCPCodeBuilder

    print_rule(f"构建代码包: {mcp_name}")
    builder = MCPCodeBuilder(mcp_path, config={"no_cache": no_cache})
    with capture_standard_output():
        build_result = builder.build()

    if not build_result.success:
        raise validation_error(build_result.error_message or "构建失败")

    artifact_reference = str(build_result.artifact_path or "")
    if push:
        print_rule("上传到 KS3")
        upload_region = _normalize_upload_region(region)
        uploader = KS3Uploader(region=upload_region, bucket=ks3_bucket)
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        object_key = f"mcps/{mcp_name}/code_{timestamp}.zip"
        with capture_standard_output():
            ks3_path = await uploader.upload(build_result.artifact_path, object_key)
        if not ks3_path:
            raise remote_error("KS3 上传失败")
        artifact_reference = ks3_path
        build_result.metadata["ks3_path"] = ks3_path
        build_result.metadata["ks3_bucket"] = uploader.bucket_name
        print_success(f"上传成功: {ks3_path}")
    else:
        print_info("跳过上传 (使用 --push 上传到 KS3)")

    return build_result, artifact_reference


def _build_container_artifact(
    *,
    mcp_path: Path,
    tag: str | None,
    registry: str | None,
    no_cache: bool,
    push: bool,
):
    from ksadk.builders.mcp_builder import MCPContainerBuilder

    print_rule("构建 Docker 镜像")
    builder = MCPContainerBuilder(
        project_dir=mcp_path,
        tag=tag,
        registry=registry,
        no_cache=no_cache,
    )
    with capture_standard_output():
        build_result = builder.build()

    if not build_result.success:
        raise validation_error(f"Docker 镜像构建失败: {build_result.error_message}")

    image_name = str(build_result.metadata.get("image") or "")
    if not image_name:
        raise validation_error("Docker 镜像构建成功，但未返回镜像地址")

    if push:
        print_rule("推送镜像到仓库")
        with capture_standard_output():
            pushed = builder.push(image_name)
        if not pushed:
            raise remote_error("镜像推送失败")
        print_success(f"推送成功: {image_name}")
    else:
        print_info("跳过推送 (使用 --push 推送镜像)")

    return build_result, image_name


def _build_mcp_request_data(
    *,
    mcp_name: str,
    config: dict,
    artifact_type: str,
    artifact_reference: str,
    region: str,
    enable_auth: bool,
    detection_result,
) -> dict:
    resources = config.get("resources") or {}
    scaling = config.get("scaling") or {}
    request_data = {
        "name": mcp_name,
        "type": "mcp",
        "artifact_type": artifact_type,
        "artifact_path": artifact_reference,
        "region": region,
        "enable_auth": enable_auth,
        "resources": {
            "cpu": str(resources.get("cpu", "1")),
            "memory": str(resources.get("memory", "2Gi")),
        },
        "scaling": {
            "min_replicas": int(scaling.get("min_replicas", 1)),
            "max_replicas": int(scaling.get("max_replicas", 5)),
            "concurrency": int(scaling.get("concurrency", 20)),
        },
        "metadata": {
            "mcp_variable": detection_result.mcp_variable,
            "tools": list(detection_result.tools or []),
        },
    }

    if artifact_type == "Code":
        from ksadk.common.auth import AWSV4Auth

        auth = AWSV4Auth()
        bucket = _parse_ks3_bucket(artifact_reference)
        request_data["ks3"] = {
            "access_key": auth.access_key_id or None,
            "secret_key": auth.secret_access_key or None,
            "region": _normalize_upload_region(region),
            "bucket": bucket,
        }
    else:
        kcr_username = os.getenv("KCR_USERNAME", "") or os.getenv("KSYUN_ACCOUNT_ID", "")
        kcr_password = os.getenv("KCR_PASSWORD")
        if kcr_username and kcr_password:
            request_data["image_credential"] = {
                "endpoint": os.getenv("KCR_ENDPOINT", "ghcr.io"),
                "username": kcr_username,
                "password": kcr_password,
            }
            print_kv("镜像凭证", f"{kcr_username}@{request_data['image_credential']['endpoint']}")
        else:
            print_warn("未配置镜像凭证 (KCR_USERNAME/KCR_PASSWORD)，私有镜像可能无法拉取")

    return request_data


async def _build_mcp_async(
    *,
    mcp_dir: str,
    artifact_type: str,
    push: bool,
    region: str,
    ks3_bucket: str | None,
    tag: str | None,
    registry: str | None,
    no_cache: bool,
):
    """异步 MCP 构建流程。"""
    from ksadk.detection.mcp_detector import MCPDetector

    mcp_path = Path(mcp_dir).resolve()
    config = _load_mcp_config(mcp_path)
    normalized_artifact_type = _normalize_artifact_type(artifact_type)
    mcp_name = _normalize_mcp_name(None, mcp_path, config)

    print_workflow_header(
        title="MCP 构建",
        subtitle=f"mode: {normalized_artifact_type}",
        project_dir=mcp_path,
        target="build",
        region=region,
        mode_label="构建模式",
        mode_value=normalized_artifact_type,
    )

    detector = MCPDetector(str(mcp_path))
    detection_result = detector.detect()
    if not detection_result.is_valid:
        raise validation_error(
            "未检测到 FastMCP 项目。",
            hints=["请确保项目包含 `from fastmcp import FastMCP`。"],
        )
    _print_mcp_detection(detection_result)
    print_kv("构建名称", mcp_name)

    if not push:
        try:
            if clear_build_metadata(mcp_path):
                print_info("当前构建未发布远端制品，已清理旧的可复用部署引用")
        except Exception as e:
            print_warn(f"清理旧构建元数据失败: {e}")

    tag = _default_container_tag(config, tag)
    registry = _default_container_registry(config, registry)

    if normalized_artifact_type == "Code":
        build_result, artifact_reference = await _build_code_artifact(
            mcp_path=mcp_path,
            mcp_name=mcp_name,
            region=region,
            ks3_bucket=ks3_bucket,
            no_cache=no_cache,
            push=push,
        )
    else:
        build_result, artifact_reference = _build_container_artifact(
            mcp_path=mcp_path,
            tag=tag,
            registry=registry,
            no_cache=no_cache,
            push=push,
        )

    if push:
        _persist_mcp_build_metadata(
            mcp_path,
            build_result,
            artifact_type=normalized_artifact_type,
            artifact_reference=artifact_reference,
        )

    _print_mcp_build_summary(
        artifact_type=normalized_artifact_type,
        build_result=build_result,
        artifact_reference=artifact_reference,
        push=push,
    )

    return {
        "framework": "mcp",
        "artifact_type": normalized_artifact_type.lower(),
        "artifact_source": (
            "built_and_uploaded"
            if normalized_artifact_type == "Code" and push
            else "built_and_pushed" if normalized_artifact_type == "Container" and push
            else "built"
        ),
        "artifact_reused": bool(build_result.metadata.get("reused", False)),
        "artifact_built": True,
        "artifact_reference": str(artifact_reference or ""),
        "artifact_path": str(build_result.artifact_path or ""),
        "image": str(build_result.metadata.get("image") or ""),
        "push": bool(push),
        "region": region,
        "mcp_name": mcp_name,
        "tools": list(detection_result.tools or []),
    }


async def _deploy_mcp_async(
    mcp_dir: str,
    name: str | None,
    region: str,
    ks3_bucket: str | None,
    enable_auth: bool,
    dry_run: bool,
    artifact_type: str,
    no_cache: bool = False,
    enable_public_access: bool | None = None,
    enable_vpc_access: bool = False,
    vpc_id: str | None = None,
    subnet_id: str | None = None,
    security_group_id: str | None = None,
    availability_zone: str | None = None,
    dry_run_context: dict[str, object] | None = None,
):
    """异步 MCP 部署流程。"""
    from ksadk.api import AgentEngineClient
    from ksadk.deployment.state import load_state, save_state
    from ksadk.detection.mcp_detector import MCPDetector

    mcp_path = Path(mcp_dir).resolve()
    config = _load_mcp_config(mcp_path)
    normalized_artifact_type = _normalize_artifact_type(artifact_type)
    mcp_name = _normalize_mcp_name(name, mcp_path, config)
    account_id = os.getenv("KSYUN_ACCOUNT_ID", "")
    predicted_registry = _default_container_registry(config, None)

    print_workflow_header(
        title="MCP 部署",
        subtitle="target: serverless",
        project_dir=mcp_path,
        target="serverless",
        region=region,
        mode_label="部署模式",
        mode_value=normalized_artifact_type,
        account_id=account_id or None,
    )

    detector = MCPDetector(str(mcp_path))
    detection_result = detector.detect()
    if not detection_result.is_valid:
        raise validation_error(
            "未检测到 FastMCP 项目。",
            hints=["请确保项目包含 `from fastmcp import FastMCP`。"],
        )

    _print_mcp_detection(detection_result)
    print_kv("部署名称", mcp_name)

    artifact_plan = plan_artifact_build(
        target="serverless",
        artifact_type=normalized_artifact_type,
        ks3_path=None,
        image=None,
        no_cache=no_cache,
    )
    if artifact_plan.should_clear_metadata:
        try:
            if clear_build_metadata(mcp_path):
                print_warn("已清除旧构建元数据 (--no-cache)")
        except Exception as e:
            print_warn(f"清理旧构建元数据失败: {e}")
    emit_no_cache_hint(plan=artifact_plan, no_cache=no_cache)

    cached_artifact_reference = None
    if not artifact_plan.should_clear_metadata:
        cached_artifact_reference = load_cached_artifact_reference(mcp_path, normalized_artifact_type)

    resolved_artifact_plan = resolve_artifact_build_plan(
        plan=artifact_plan,
        target="serverless",
        artifact_type=normalized_artifact_type,
        dry_run=dry_run,
        deploy_name=mcp_name,
        region=region,
        account_id=account_id,
        ks3_bucket=ks3_bucket,
        registry=predicted_registry,
        explicit_reference=None,
        cached_reference=cached_artifact_reference,
    )
    if resolved_artifact_plan.reference_is_predicted:
        resolved_artifact_plan = replace(
            resolved_artifact_plan,
            reference=_predict_mcp_artifact_reference(
                artifact_type=normalized_artifact_type,
                mcp_name=mcp_name,
                region=region,
                ks3_bucket=ks3_bucket,
                registry=predicted_registry,
                account_id=account_id,
            ),
        )

    state = load_state(mcp_path)
    existing_mcp_id = state.get("mcp_id") if state.get("type") == "mcp" else None

    print_rule("Step 1/2 准备制品")
    artifact_reference = str(resolved_artifact_plan.reference or "")

    if not resolved_artifact_plan.will_build and artifact_reference:
        source_label = {
            "cached": "缓存制品",
            "planned_build": "预测制品",
        }.get(resolved_artifact_plan.source or "", "制品引用")
        print_kv(source_label, artifact_reference)
        if resolved_artifact_plan.source == "cached":
            print_info("检测到缓存制品，跳过重新构建")
        elif resolved_artifact_plan.source == "planned_build":
            print_info("Dry Run: 仅生成本地构建计划，不执行真实构建/上传")

    if resolved_artifact_plan.will_build:
        print_rule("Step 1/2 构建与发布")
        if normalized_artifact_type == "Code":
            build_result, artifact_reference = await _build_code_artifact(
                mcp_path=mcp_path,
                mcp_name=mcp_name,
                region=region,
                ks3_bucket=ks3_bucket,
                no_cache=no_cache,
                push=True,
            )
        else:
            build_result, artifact_reference = _build_container_artifact(
                mcp_path=mcp_path,
                tag=_default_container_tag(config, None),
                registry=predicted_registry,
                no_cache=no_cache,
                push=True,
            )
        _persist_mcp_build_metadata(
            mcp_path,
            build_result,
            artifact_type=normalized_artifact_type,
            artifact_reference=artifact_reference,
        )

    local_plan = build_workflow_local_plan(
        project_dir=mcp_path,
        framework="mcp",
        target="serverless",
        region=region,
        deploy_name=mcp_name,
        artifact_type=normalized_artifact_type,
        artifact_plan=resolved_artifact_plan,
        build_dir=str(mcp_path / ".agentengine"),
        artifact_reference=str(artifact_reference),
        no_cache=no_cache,
    )
    if dry_run_context is not None:
        dry_run_context["plan"] = local_plan

    print_rule("Step 2/2 部署 MCP Server")
    request_data = _build_mcp_request_data(
        mcp_name=mcp_name,
        config=config,
        artifact_type=normalized_artifact_type,
        artifact_reference=artifact_reference,
        region=region,
        enable_auth=enable_auth,
        detection_result=detection_result,
    )
    network_payload = build_network_payload(
        enable_public_access=enable_public_access,
        enable_vpc_access=enable_vpc_access,
        vpc_id=vpc_id,
        subnet_id=subnet_id,
        security_group_id=security_group_id,
        availability_zone=availability_zone,
    )
    if network_payload:
        request_data["network"] = network_payload

    async with AgentEngineClient(region=region, dry_run=dry_run) as client:
        if existing_mcp_id:
            print_info(f"检测到本地状态: {existing_mcp_id}")
            print_info("执行热更新...")
            res = await client.update_mcp(existing_mcp_id, request_data)
            mcp_id = existing_mcp_id
        else:
            res = await client.create_mcp(request_data)
            if not res:
                raise remote_error("Server 返回空响应，请检查 MCP 名称是否冲突或服务端日志。")
            mcp_id = res.get("mcp_id")

    endpoint = res.get("endpoint")

    state_data = {
        "type": "mcp",
        "mcp_id": mcp_id,
        "name": mcp_name,
        "region": region,
        "artifact_type": normalized_artifact_type,
        "endpoint": endpoint,
        "mcp_endpoint": f"{endpoint}/mcp" if endpoint else None,
        "tools": list(detection_result.tools or []),
    }
    if res.get("api_key"):
        state_data["api_key"] = res.get("api_key")

    save_state(mcp_path, state_data)
    print_info("已保存状态到 .agentengine.state")

    print_success("MCP 部署成功")
    print_kv("MCP ID", mcp_id)
    if endpoint:
        print_kv("Endpoint", endpoint, value_style="#58a6ff")
        print_kv("MCP URL", f"{endpoint}/mcp", value_style="#58a6ff")
    print_rule("调用方式")
    print_info('# Cursor/Claude: {"url": "<endpoint>/mcp"}')
    print_info('LangChain/LangGraph: MCPClientToolkit(url="<endpoint>/mcp")')
    print_info('ADK: MCPToolset.from_server(connection_params={"url": "<endpoint>/mcp"})')

    return {
        "framework": "mcp",
        "artifact_type": normalized_artifact_type.lower(),
        "artifact_source": str(resolved_artifact_plan.source or ""),
        "artifact_reused": bool((resolved_artifact_plan.source or "") == "cached"),
        "artifact_built": bool(resolved_artifact_plan.will_build),
        "artifact_reference": str(artifact_reference or ""),
        "mcp_id": str(mcp_id or ""),
        "mcp_name": str(mcp_name),
        "endpoint": str(endpoint or ""),
        "mcp_url": str(f"{endpoint}/mcp" if endpoint else ""),
        "api_key_present": bool(res.get("api_key")),
        "status": "DEPLOYED",
        "region": region,
        "tools": list(detection_result.tools or []),
    }


@mcp.command("list", context_settings=CONTEXT_SETTINGS)
@region_option()
@pagination_options(default_page=1, default_size=20)
@dry_run_option()
@cli_output_option()
def list_mcps(region: str, page: int, size: int, dry_run: bool, output_mode: str | None):
    """列出已部署的 MCP"""
    _ = output_mode
    dry_run = effective_dry_run(dry_run)
    from ksadk.api import AgentEngineClient
    
    async def _list():
        async with AgentEngineClient(region=region, dry_run=dry_run) as client:
            resp = await client.list_mcps(region=region, page=page, page_size=size)
            mcps = resp.get("mcps", [])
            total = int(resp.get("total", len(mcps)) or 0)

            rows = []
            items = []
            for m in mcps:
                status = (m.get("status") or "UNKNOWN").upper()
                rows.append(
                    (
                        str(m.get("mcp_id", "-")),
                        str(m.get("name", "-")),
                        f"[{status_rich_style(status)}]{status}[/]",
                        str(m.get("mcp_endpoint", "N/A")),
                    )
                )
                items.append(
                    {
                        "id": str(m.get("mcp_id", "-")),
                        "name": str(m.get("name", "-")),
                        "status": status,
                        "mcp_url": str(m.get("mcp_endpoint", "N/A")),
                    }
                )

            if not render_descriptor_list(
                MCP_RESOURCE,
                rows=rows,
                total=total,
                page=page,
                size=size,
                items=items,
            ):
                return

    try:
        run_async_with_dry_run(
            _list(),
            dry_run=dry_run,
            dry_run_resource="mcp",
            dry_run_action="list",
        )
    except Exception as e:
        _abort_mcp_error(e, context="获取列表失败", argv=["mcp", "list"])


@mcp.command("status", context_settings=CONTEXT_SETTINGS)
@click.argument("mcp_ref", required=False)
@region_option(default=None, envvar=None, help_text="区域 (默认优先读取 .agentengine.state)")
@dry_run_option()
@cli_output_option()
def status(mcp_ref: str | None, region: str | None, dry_run: bool, output_mode: str | None):
    """查看 MCP 状态
    
    MCP_ID: MCP 的 ID
    """
    _ = output_mode
    dry_run = effective_dry_run(dry_run)
    from ksadk.api import AgentEngineClient
    from ksadk.deployment.state import load_state

    cwd = Path(".").resolve()
    state = load_state(cwd)
    region = region or state.get("region") or os.getenv("KSYUN_REGION") or "cn-beijing-6"
    resolved = resolve_mcp_ref(mcp_ref, cwd=cwd, include_state=True)
    if not resolved:
        _abort_mcp_error(
            resolution_error(
                MCP_RESOURCE.missing_ref_message or "请指定 MCP。",
                hints=list(MCP_RESOURCE.resolution_commands),
            ),
            argv=["mcp", "status"],
        )
        return
    target_ref = resolved.value
    if resolved.source != "cli":
        print_info(f"未显式指定 MCP，使用 {resolved.source_text}: {target_ref}")
    
    async def _get():
        async with AgentEngineClient(region=region, dry_run=dry_run) as client:
            mcp = None
            try:
                mcp = await client.get_mcp(target_ref)
            except DryRunExit:
                raise
            except Exception:
                mcp = await client.get_mcp_by_name(target_ref, region=region)
            if not mcp:
                raise resolution_error(f"未找到 MCP: {target_ref}", hints=["agentengine mcp list"])
            
            status_text = (mcp.get("status") or "UNKNOWN").upper()
            fields = [
                ("ID", str(mcp.get("mcp_id", "-")), None),
                ("状态", status_text, status_rich_style(status_text)),
                ("区域", str(mcp.get("region", region)), None),
                ("Endpoint", str(mcp.get("endpoint", "N/A")), "#58a6ff"),
                ("MCP URL", str(mcp.get("mcp_endpoint", "N/A")), "#58a6ff"),
                ("认证", "已开启" if mcp.get("enable_auth") else "未开启", None),
            ]
            if mcp.get('tools'):
                fields.append(("工具", ", ".join(mcp["tools"]), None))
            fields.extend(
                [
                    ("创建时间", str(mcp.get("created_at")), None),
                    ("更新时间", str(mcp.get("updated_at")), None),
                ]
            )
            render_descriptor_status(
                MCP_RESOURCE,
                subtitle=str(mcp.get("name", target_ref)),
                fields=fields,
                item={
                    "id": str(mcp.get("mcp_id", "-")),
                    "name": str(mcp.get("name", target_ref)),
                    "status": status_text,
                    "region": str(mcp.get("region", region)),
                    "endpoint": str(mcp.get("endpoint", "N/A")),
                    "mcp_url": str(mcp.get("mcp_endpoint", "N/A")),
                    "auth_enabled": bool(mcp.get("enable_auth")),
                    "tools": list(mcp.get("tools") or []),
                    "created_at": str(mcp.get("created_at")),
                    "updated_at": str(mcp.get("updated_at")),
                },
            )

    try:
        run_async_with_dry_run(
            _get(),
            dry_run=dry_run,
            dry_run_resource="mcp",
            dry_run_action="status",
        )
    except Exception as e:
        _abort_mcp_error(e, context="获取状态失败", argv=["mcp", "status"])


def _delete_impl(mcp_ids: tuple[str, ...], region: str, assume_yes: bool, dry_run: bool):
    """删除 MCP。

    MCP_ID: 要删除的 MCP ID
    """
    dry_run = effective_dry_run(dry_run)
    from ksadk.api import AgentEngineClient

    if not confirm_destructive(
        assume_yes=assume_yes,
        dry_run=dry_run,
        prompt=f"确定要删除这 {len(mcp_ids)} 个 MCP 吗?",
    ):
        return
    
    async def _delete():
        async with AgentEngineClient(region=region, dry_run=dry_run) as client:
            failed_ids: list[str] = []
            deleted_ids: list[str] = []
            for mcp_id in mcp_ids:
                success = await client.delete_mcp(mcp_id)
                if success:
                    deleted_ids.append(mcp_id)
                    print_success(f"MCP 已删除: {mcp_id}")

                    # 尝试清理本地状态文件 (如果在项目目录中)
                    from ksadk.deployment.state import clear_state
                    try:
                        removed = clear_state(Path("."), key=mcp_id)
                        if removed:
                            print_info("本地状态文件已清理")
                        else:
                            print_warn("未清理本地状态文件: 当前目录状态与目标 ID 不匹配")
                    except Exception:
                        pass
                else:
                    failed_ids.append(mcp_id)

            if failed_ids:
                raise remote_error(
                    f"以下 MCP 删除失败: {', '.join(failed_ids)}",
                    details={"deleted": deleted_ids, "failed": failed_ids},
                )
            return {
                "targets": list(mcp_ids),
                "deleted": deleted_ids,
                "failed": failed_ids,
            }

    dry_run_kwargs = {"dry_run": dry_run}
    if is_json_output():
        dry_run_kwargs.update(
            dry_run_resource="mcp",
            dry_run_action="delete",
        )
    try:
        result = run_async_with_dry_run(_delete(), **dry_run_kwargs)
    except Exception as e:
        _abort_mcp_error(e, context="删除失败", argv=["mcp", "delete"])
        return
    if result is not None:
        deleted_text = ", ".join(result["deleted"]) or "-"
        failed_text = ", ".join(result["failed"]) or "-"
        render_descriptor_status(
            MCP_RESOURCE,
            title="MCP 删除结果",
            subtitle=", ".join(result["targets"]) if result["targets"] else "-",
            fields=[
                ("目标数量", str(len(result["targets"])), None),
                ("已删除", deleted_text, "ok" if result["deleted"] else "muted"),
                ("失败", failed_text, "err" if result["failed"] else "muted"),
            ],
            next_steps=(
                "agentengine mcp list",
                "agentengine mcp deploy",
            ),
            action="delete",
            item=result,
        )


@mcp.command("delete", context_settings=CONTEXT_SETTINGS)
@click.argument("mcp_ids", nargs=-1, required=True)
@region_option()
@confirm_options()
@dry_run_option()
@cli_output_option()
def delete(mcp_ids: tuple[str, ...], region: str, assume_yes: bool, dry_run: bool, output_mode: str | None):
    """删除 MCP。"""
    _ = output_mode
    _delete_impl(mcp_ids=mcp_ids, region=region, assume_yes=assume_yes, dry_run=dry_run)


@mcp.command("destroy", context_settings=CONTEXT_SETTINGS, hidden=True)
@click.argument("mcp_ids", nargs=-1, required=True)
@region_option()
@confirm_options()
@dry_run_option()
@cli_output_option()
def destroy(mcp_ids: tuple[str, ...], region: str, assume_yes: bool, dry_run: bool, output_mode: str | None):
    """删除 MCP。"""
    _ = output_mode
    _delete_impl(mcp_ids=mcp_ids, region=region, assume_yes=assume_yes, dry_run=dry_run)
