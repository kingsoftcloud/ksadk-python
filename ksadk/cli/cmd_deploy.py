"""
agentengine deploy - 部署 Agent 到云端

支持多种部署目标:
- serverless: 金山云 Serverless 计算引擎 (默认)
- docker: 本地 Docker 容器
- k8s: Kubernetes 集群
"""

import os
import json
import click
import asyncio
from pathlib import Path
from ksadk.api.client import DryRunExit
from ksadk.cli.storage import build_storage_config
from ksadk.common.constants import (
    get_ks3_endpoints,
    DEFAULT_SERVERLESS_ENDPOINT,
)
from ksadk.cli.dry_run import effective_dry_run, run_async_with_dry_run
from ksadk.cli.error_utils import cli_error_from_exception, is_debug_mode_enabled, remote_error, usage_error, validation_error
from ksadk.cli.network_options import (
    apply_network_cli_overrides,
    apply_network_config as _apply_network_config_shared,
    network_cli_kwargs,
    network_options,
    validate_deploy_target_network,
)
from ksadk.cli.workflow_common import (
    build_workflow_local_plan,
    clear_build_metadata,
    emit_no_cache_hint,
    load_cached_artifact_reference,
    plan_artifact_build,
    print_agent_next_steps,
    print_workflow_header,
    render_workflow_dry_run,
    render_workflow_result,
    resolve_artifact_build_plan,
    should_build_artifact as _workflow_should_build_artifact,
)
from ksadk.deployment.ui_config import SUPPORTED_UI_PROFILES, extract_ui_state
from ksadk.cli.ui import (
    capture_standard_output,
    get_console,
    is_json_output,
    new_table,
    output_option as cli_output_option,
    print_error,
    print_info,
    print_kv,
    print_rule,
    print_success,
    print_warn,
)

console = get_console()


@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.argument("agent_dir", default=".", type=click.Path(exists=True))
@click.option(
    "--target",
    "-t",
    type=click.Choice(["serverless", "kcf", "kce"]),
    default="serverless",
    help="部署目标 (default: serverless)",
)
@click.option("--name", "-n", help="部署名称")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域 (default: cn-beijing-6)")
@click.option("--account-id", envvar="KSYUN_ACCOUNT_ID", help="金山云账号 ID")
@click.option(
    "--artifact-type",
    type=click.Choice(["Code", "Container"]),
    default="Code",
    help="Serverless 部署模式 (default: Code)",
)
@click.option("--namespace", default="default", help="K8s 命名空间")
@click.option("--port", "-p", default=8000, help="服务端口 (default: 8000)")
@click.option("--registry", help="镜像仓库地址 (k8s/serverless Container 模式)")
@click.option("--ks3-path", help="KS3 代码包路径 (Serverless Code 模式)")
@click.option("--ks3-bucket", help="KS3 bucket 名称 (Serverless Code 模式, 默认: agentengine-{region})")
@click.option("--image", help="Docker 镜像地址 (Container 模式)")
@click.option("--ui-profile", type=click.Choice(SUPPORTED_UI_PROFILES), help="Dashboard UI 类型")
@click.option("--ui-path", help="Dashboard UI 路径 (例如 /)")
@click.option("--ui-url", help="完整 Dashboard URL（自研前端）")
@click.option("--storage-size-gi", type=int, default=20, show_default=True, help="PVC 容量（Gi）")
@click.option("--storage-mount-path", default=None, help="PVC 挂载目录（默认按框架推导）")
@click.option("--no-storage", is_flag=True, help="禁用默认 PVC 挂载")
@network_options
@click.option(
    "--observability/--no-observability", default=True, help="是否启用可观测性 (默认开启)"
)
@click.option("--push", is_flag=True, help="构建后推送镜像")
@click.option("--no-cache", is_flag=True, help="强制重新构建，不使用缓存")
@click.option("--repackage", is_flag=True, help="Code 模式复用依赖缓存，但强制重新打包当前代码/runtime")
@click.option("--no-version", is_flag=True, help="部署成功后不自动创建版本快照")
@click.option("--auto-rollback", is_flag=True, help="部署失败时自动回滚到上一版本")
@click.option("--dry-run", is_flag=True, help="只生成配置，打印 curl 请求，不执行部署")
@click.option("--list-providers", "list_providers", is_flag=True, help="列出可用的部署目标")
@cli_output_option()
def deploy(
    agent_dir: str,
    target: str,
    name: str,
    region: str,
    account_id: str,
    artifact_type: str,
    namespace: str,
    port: int,
    registry: str,
    ks3_path: str,
    ks3_bucket: str,
    image: str,
    ui_profile: str,
    ui_path: str,
    ui_url: str,
    storage_size_gi: int,
    storage_mount_path: str | None,
    no_storage: bool,
    enable_public_access: bool | None,
    enable_vpc_access: bool,
    vpc_id: str | None,
    subnet_id: str | None,
    security_group_id: str | None,
    availability_zone: str | None,
    observability: bool,
    push: bool,
    no_cache: bool,
    repackage: bool,
    no_version: bool,
    auto_rollback: bool,
    dry_run: bool,
    list_providers: bool,
    output_mode: str | None,
):
    """部署 Agent 到云端

    \b
    AGENT_DIR: Agent 项目目录 (默认: 当前目录)

    \b
    示例:
        # 1) 默认部署 (serverless)
        agentengine deploy .
        # 2) 显式指定部署参数
        agentengine deploy . --target kcf --account-id X-Ksc-Account-Id
        # 3) 显式指定区域
        KSYUN_REGION=cn-beijing-6 agentengine deploy . --target serverless --dry-run
    """
    dry_run = effective_dry_run(dry_run)

    # 列出 Provider
    if list_providers:
        _list_providers()
        return

    # 执行部署
    _ = output_mode
    dry_run_context: dict[str, object] = {}
    result = run_async_with_dry_run(
        _deploy_async(
            agent_dir,
            target,
            name,
            region,
            account_id,
            artifact_type,
            namespace,
            port,
            registry,
            ks3_path,
            ks3_bucket,
            image,
            ui_profile,
            ui_path,
            ui_url,
            observability,
            push,
            no_cache,
            no_version,
            auto_rollback,
            dry_run,
            storage_size_gi,
            storage_mount_path,
            no_storage,
            **network_cli_kwargs(
                enable_public_access=enable_public_access,
                enable_vpc_access=enable_vpc_access,
                vpc_id=vpc_id,
                subnet_id=subnet_id,
                security_group_id=security_group_id,
                availability_zone=availability_zone,
            ),
            repackage=repackage,
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
    if result is not None and is_json_output():
        render_workflow_result(action="deploy", result=result)




def _list_providers():

    """列出所有可用的部署 Provider"""
    from ksadk.deployment import DeploymentManager

    providers = DeploymentManager.list_providers()

    table = new_table("可用的部署目标")
    table.add_column("名称", style="#58a6ff", no_wrap=True)
    table.add_column("显示名", style="white")
    table.add_column("说明", style="#8b949e")
    table.add_column("特性", style="#c9d1d9")

    for p in providers:
        features = []
        if p.get("supports_scaling"):
            features.append("扩缩容")
        if p.get("supports_streaming"):
            features.append("流式")
        if p.get("requires_image_registry"):
            features.append("需镜像仓库")

        table.add_row(
            p["name"],
            p.get("display_name", p["name"]),
            p.get("description", "-"),
            ", ".join(features) if features else "-",
        )

    console.print(table)


async def _deploy_async(
    agent_dir: str,
    target: str,
    name: str,
    region: str,
    account_id: str,
    artifact_type: str,
    namespace: str,
    port: int,
    registry: str,
    ks3_path: str,
    ks3_bucket: str,
    image: str,
    ui_profile: str,
    ui_path: str,
    ui_url: str,
    observability: bool,
    push: bool,
    no_cache: bool,
    no_version: bool,
    auto_rollback: bool,
    dry_run: bool,
    storage_size_gi: int = 20,
    storage_mount_path: str | None = None,
    no_storage: bool = False,
    enable_public_access: bool | None = None,
    enable_vpc_access: bool = False,
    vpc_id: str | None = None,
    subnet_id: str | None = None,
    security_group_id: str | None = None,
    availability_zone: str | None = None,
    dry_run_context: dict[str, object] | None = None,
    *,
    repackage: bool = False,
):
    """异步部署流程"""
    from ksadk.detection import FrameworkDetector
    from ksadk.deployment import DeploymentManager, DeployTarget, DeployStatus

    agent_path = Path(agent_dir).resolve()
    config = _load_config(agent_path)
    effective_artifact_type = _resolve_artifact_type_input(config, artifact_type)
    print_workflow_header(
        title="Agent 部署",
        subtitle=f"target: {target}",
        project_dir=agent_path,
        target=target,
        region=region,
        mode_label="部署模式" if target == "serverless" else None,
        mode_value=effective_artifact_type if target == "serverless" else None,
        account_id=account_id,
        observability=observability if target == "serverless" else None,
    )

    # 1. 检测框架
    detector = FrameworkDetector(str(agent_path))
    detection_result = detector.detect()

    if detection_result.type.value == "unknown":
        raise validation_error("未检测到支持的框架")

    print_kv("框架", detection_result.name, value_style="#2da44e")

    if detection_result.type.value == "hermes":
        raise usage_error(
            "Hermes 项目请使用 `agentengine hermes deploy`，不要使用通用 `agentengine deploy`。",
            hints=[
                "Hermes 部署会走专用模板和镜像构建流程。",
                "可先执行 `agentengine hermes --help` 查看专用命令。",
            ],
            argv=["deploy"],
        )
    if detection_result.type.value == "openclaw":
        raise usage_error(
            "OpenClaw 项目请使用 `agentengine openclaw deploy`，不要使用通用 `agentengine deploy`。",
            hints=[
                "OpenClaw 部署会走专用网关与容器配置流程。",
                "可先执行 `agentengine openclaw --help` 查看专用命令。",
            ],
            argv=["deploy"],
        )

    # 2. 确定部署名称
    deploy_name = name or config.get("name") or agent_path.name.replace("-", "_").replace(".", "_")
    print_kv("部署名称", deploy_name)
    resolved_ui_profile, resolved_ui_path, resolved_ui_url = _resolve_ui_config_inputs(
        config,
        ui_profile=ui_profile,
        ui_path=ui_path,
        ui_url=ui_url,
    )

    # 4. 获取 Provider
    try:
        provider = DeploymentManager.get_provider(target)
    except ValueError as e:
        raise usage_error(str(e), hints=["使用 `agentengine deploy --list-providers` 查看可用目标。"])

    # 5. 构建部署目标配置
    deploy_target = DeployTarget(
        provider=target,
        region=region,
        project_id=config.get("project_id", "default"),
        extra={
            "account_id": account_id,
            "namespace": namespace,
            "port": port,
            "registry": registry or config.get("image", {}).get("registry", ""),
            "kubeconfig": config.get("deploy", {}).get("k8s", {}).get("kubeconfig"),
            "artifact_type": effective_artifact_type,
            "ks3_path": ks3_path,
            "ks3_bucket": ks3_bucket,
            "image": image,
            "ui_profile": resolved_ui_profile,
            "ui_path": resolved_ui_path,
            "ui_url": resolved_ui_url,
            "enable_observability": observability,
            "dry_run": dry_run,
            "no_cache": no_cache,
            "repackage": repackage,
        },
    )

    artifact_plan = plan_artifact_build(
        target=target,
        artifact_type=effective_artifact_type,
        ks3_path=ks3_path,
        image=image,
        no_cache=no_cache,
        repackage=repackage,
    )
    if artifact_plan.should_clear_metadata:
        try:
            if clear_build_metadata(agent_path):
                print_warn("已清除旧构建元数据 (--no-cache)")
        except Exception as e:
            print_warn(f"清理旧构建元数据失败: {e}")
    emit_no_cache_hint(plan=artifact_plan, no_cache=no_cache)

    # 更新资源配置
    if "resources" in config:
        deploy_target.resources.cpu = config["resources"].get("cpu", "2")
        deploy_target.resources.memory = config["resources"].get("memory", "4Gi")

    if "scaling" in config:
        deploy_target.scaling.min_replicas = config["scaling"].get("min_replicas", 1)
        deploy_target.scaling.max_replicas = config["scaling"].get("max_replicas", 10)
        deploy_target.scaling.concurrency = config["scaling"].get("concurrency", 10)

    _apply_network_config(config, deploy_target)
    apply_network_cli_overrides(
        deploy_target,
        enable_public_access=enable_public_access,
        enable_vpc_access=enable_vpc_access,
        vpc_id=vpc_id,
        subnet_id=subnet_id,
        security_group_id=security_group_id,
        availability_zone=availability_zone,
    )
    validate_deploy_target_network(deploy_target)
    storage_config = build_storage_config(
        detection_result.type.value,
        target=target,
        no_storage=no_storage,
        mount_path=storage_mount_path,
        size_gi=storage_size_gi,
    )
    if storage_config:
        deploy_target.storage.mount_path = storage_config["mount_path"]
        deploy_target.storage.size_gi = storage_config["size_gi"]

    normalized_artifact_type = (effective_artifact_type or "Code").strip().lower()
    explicit_artifact_reference = ks3_path if normalized_artifact_type == "code" else image
    cached_artifact_reference = None
    if not artifact_plan.should_clear_metadata:
        cached_artifact_reference = load_cached_artifact_reference(agent_path, effective_artifact_type)
    resolved_artifact_plan = resolve_artifact_build_plan(
        plan=artifact_plan,
        target=target,
        artifact_type=effective_artifact_type,
        dry_run=dry_run,
        deploy_name=deploy_name,
        region=region,
        account_id=account_id,
        ks3_bucket=ks3_bucket,
        registry=registry or config.get("image", {}).get("registry", ""),
        explicit_reference=explicit_artifact_reference,
        cached_reference=cached_artifact_reference,
    )

    # 6. 验证配置
    with capture_standard_output():
        valid, error_msg = await provider.validate_config(deploy_target)
    if not valid:
        raise validation_error(f"配置验证失败: {error_msg}")

    # 7. 打包 (Package 步骤仍需保留以获取框架信息等，但不构建制品)
    total_steps = 3 if resolved_artifact_plan.will_build else 2
    print_rule(f"Step 1/{total_steps} 准备配置")
    try:
        with capture_standard_output():
            package_info = await provider.package(str(agent_path), detection_result, config)
        package_info.name = deploy_name

        if resolved_artifact_plan.reference:
            if normalized_artifact_type == "container":
                package_info.image = resolved_artifact_plan.reference
                package_info.metadata["image"] = resolved_artifact_plan.reference
            else:
                package_info.metadata["ks3_path"] = resolved_artifact_plan.reference

        print_kv("构建目录", str(package_info.build_dir))
        print_kv("框架", package_info.framework)
    except Exception as e:
        raise cli_error_from_exception(e, context="打包失败")

    if not resolved_artifact_plan.will_build and resolved_artifact_plan.reference:
        source_label = {
            "external": "外部制品",
            "cached": "缓存制品",
            "planned_build": "预测制品",
        }.get(resolved_artifact_plan.source or "", "制品引用")
        print_kv(source_label, resolved_artifact_plan.reference)
        if resolved_artifact_plan.source == "cached":
            print_info("检测到缓存制品，跳过重新构建")
        elif resolved_artifact_plan.source == "planned_build":
            print_info("Dry Run: 仅生成本地构建计划，不执行真实构建/上传")

    if resolved_artifact_plan.will_build:
        print_rule(f"Step 2/{total_steps} 构建与上传")
        try:
            with capture_standard_output():
                package_info = await provider.build(package_info, deploy_target)

            if target == "serverless":
                if effective_artifact_type == "Code":
                    ks3 = package_info.metadata.get("ks3_path")
                    if ks3:
                        print_kv("KS3 路径", ks3)
                elif package_info.image:
                    print_kv("镜像", package_info.image)
        except Exception as e:
            raise cli_error_from_exception(e, context="构建失败")

    artifact_reference = (
        package_info.metadata.get("ks3_path")
        or package_info.image
        or resolved_artifact_plan.reference
        or ks3_path
        or image
        or ""
    )
    local_plan = build_workflow_local_plan(
        project_dir=agent_path,
        framework=detection_result.name,
        target=target,
        region=region,
        deploy_name=deploy_name,
        artifact_type=effective_artifact_type,
        artifact_plan=resolved_artifact_plan,
        build_dir=str(package_info.build_dir),
        artifact_reference=str(artifact_reference),
        no_cache=no_cache,
        repackage=repackage,
    )
    if dry_run_context is not None:
        dry_run_context["plan"] = local_plan

    # 8. 部署
    print_rule(f"Step {total_steps}/{total_steps} 部署到 {target}")
    try:
        with capture_standard_output():
            result = await provider.deploy(package_info, deploy_target)

        if result.is_success():
            print_success("部署成功")
            print_rule()
            print_kv("名称", result.agent_name or deploy_name)
            if result.agent_id:
                print_kv("ID", result.agent_id)
            print_kv("状态", result.status.value, value_style="#2da44e")
            if result.endpoint:
                print_kv("Endpoint", result.endpoint, value_style="#58a6ff")
            if result.api_key:
                print_kv("API Key", result.api_key, value_style="#d29922")
                # 首次部署提示 API Key 仅显示一次
                if result.message and "首次部署" in result.message:
                    print_warn("⚠️  API Key 仅在首次部署时明文显示，请妥善保存！")
            if result.message:
                print_kv("信息", result.message)
            
            # 9. 自动创建版本快照 (仅热更新时，首次部署平台自动创建 v1)
            is_update = result.message and "已更新" in result.message
            if result.agent_id and is_update and not no_version and not dry_run:
                from ksadk.cli.deploy_utils import auto_release_version
                with capture_standard_output():
                    await auto_release_version(result.agent_id, region, deploy_name)

            target_ref = result.agent_id or deploy_name
            print_agent_next_steps(target_ref)
            return {
                "framework": detection_result.name,
                "artifact_type": effective_artifact_type.lower(),
                "artifact_source": str(resolved_artifact_plan.source or ""),
                "artifact_reused": bool((resolved_artifact_plan.source or "") == "cached"),
                "artifact_built": bool(resolved_artifact_plan.will_build),
                "artifact_reference": str(
                    package_info.metadata.get("ks3_path")
                    or package_info.image
                    or resolved_artifact_plan.reference
                    or ks3_path
                    or image
                    or ""
                ),
                "agent_id": str(result.agent_id or ""),
                "agent_name": str(result.agent_name or deploy_name),
                "endpoint": str(result.endpoint or ""),
                "api_key_present": bool(result.api_key),
                "status": result.status.value,
                "message": str(result.message or ""),
                "region": region,
                "target": target,
            }
        else:
            # 可能是 DryRun 的 SKIPPED
            if result.status.name == "SKIPPED":
                dry_run_request = result.metadata.get("dry_run_request") if isinstance(result.metadata, dict) else None
                if dry_run and dry_run_request:
                    raise DryRunExit(result.message or "Dry Run finished.", payload=dry_run_request)
                print_warn(f"部署状态: {result.status.value}")
                return {
                    "framework": detection_result.name,
                    "artifact_type": effective_artifact_type.lower(),
                    "artifact_source": str(resolved_artifact_plan.source or ""),
                    "artifact_reused": bool((resolved_artifact_plan.source or "") == "cached"),
                    "artifact_built": bool(resolved_artifact_plan.will_build),
                    "artifact_reference": str(
                        package_info.metadata.get("ks3_path")
                        or package_info.image
                        or resolved_artifact_plan.reference
                        or ks3_path
                        or image
                        or ""
                    ),
                    "agent_id": str(result.agent_id or ""),
                    "agent_name": str(result.agent_name or deploy_name),
                    "endpoint": str(result.endpoint or ""),
                    "api_key_present": bool(result.api_key),
                    "status": result.status.value,
                    "message": str(result.message or ""),
                    "region": region,
                    "target": target,
                }
            else:
                raise remote_error(
                    f"部署状态: {result.status.value}",
                    details={
                        "status": result.status.value,
                        "message": result.message or "",
                        "agent_id": result.agent_id or "",
                    },
                )
            if result.message:
                print_info(result.message)
            
            # 10. 部署失败时自动回滚 (如果启用了 --auto-rollback)
            if auto_rollback and result.agent_id and result.status.name not in ["SKIPPED"]:
                from ksadk.cli.deploy_utils import auto_rollback_to_previous
                with capture_standard_output():
                    await auto_rollback_to_previous(result.agent_id, region)
                
    except DryRunExit:
        raise
    except Exception as e:
        if is_debug_mode_enabled():
            import traceback

            traceback.print_exc()
        raise cli_error_from_exception(e, context="部署失败")


def _should_build_artifact(*, target: str, artifact_type: str, ks3_path: str, image: str) -> bool:
    """判断 deploy 是否需要本地重新构建制品。"""
    return _workflow_should_build_artifact(
        target=target,
        artifact_type=artifact_type,
        ks3_path=ks3_path,
        image=image,
    )





def _load_config(agent_path: Path) -> dict:
    """加载配置文件"""
    import yaml

    # 优先 agentengine.yaml
    config_path = agent_path / "agentengine.yaml"
    if not config_path.exists():
        config_path = agent_path / "ksadk.yaml"

    if config_path.exists():
        # 使用 utf-8-sig 自动处理 BOM，确保 Windows 兼容性
        with open(config_path, encoding='utf-8-sig') as f:
            return yaml.safe_load(f) or {}

    return {}


def _resolve_ui_config_inputs(
    config: dict,
    *,
    ui_profile: str | None,
    ui_path: str | None,
    ui_url: str | None,
) -> tuple[str | None, str | None, str | None]:
    config_profile, config_path, config_url = extract_ui_state(config if isinstance(config, dict) else None)
    return (
        ui_profile if ui_profile is not None else config_profile,
        ui_path if ui_path is not None else config_path,
        ui_url if ui_url is not None else config_url,
    )


def _resolve_artifact_type_input(config: dict, cli_artifact_type: str | None) -> str:
    raw = cli_artifact_type
    if raw is None and isinstance(config, dict):
        deploy_config = config.get("deploy") if isinstance(config.get("deploy"), dict) else {}
        raw = config.get("artifact_type") or deploy_config.get("artifact_type")
    normalized = str(raw or "Code").strip().lower()
    return "Container" if normalized == "container" else "Code"


def _apply_network_config(config: dict, deploy_target: "DeployTarget") -> None:
    _apply_network_config_shared(config, deploy_target)
