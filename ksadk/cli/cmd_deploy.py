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
from ksadk.common.constants import (
    get_ks3_endpoints,
    DEFAULT_SERVERLESS_ENDPOINT,
)
from ksadk.cli.dry_run import effective_dry_run
from ksadk.cli.ui import (
    get_console,
    new_table,
    print_error,
    print_info,
    print_kv,
    print_next_steps,
    print_rule,
    print_success,
    print_title,
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
@click.option(
    "--observability/--no-observability", default=True, help="是否启用可观测性 (默认开启)"
)
@click.option("--push", is_flag=True, help="构建后推送镜像")
@click.option("--no-cache", is_flag=True, help="强制重新构建，不使用缓存")
@click.option("--no-version", is_flag=True, help="部署成功后不自动创建版本快照")
@click.option("--auto-rollback", is_flag=True, help="部署失败时自动回滚到上一版本")
@click.option("--dry-run", is_flag=True, help="只生成配置，打印 curl 请求，不执行部署")
@click.option("--list-providers", "list_providers", is_flag=True, help="列出可用的部署目标")
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
    observability: bool,
    push: bool,
    no_cache: bool,
    no_version: bool,
    auto_rollback: bool,
    dry_run: bool,
    list_providers: bool,
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
    asyncio.run(
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
            observability,
            push,
            no_cache,
            no_version,
            auto_rollback,
            dry_run,
        )
    )




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
    observability: bool,
    push: bool,
    no_cache: bool,
    no_version: bool,
    auto_rollback: bool,
    dry_run: bool,
):
    """异步部署流程"""
    from ksadk.detection import FrameworkDetector
    from ksadk.deployment import DeploymentManager, DeployTarget, DeployStatus

    agent_path = Path(agent_dir).resolve()
    print_title("Agent 部署", f"target: {target}")
    print_kv("项目目录", str(agent_path))
    print_kv("区域", region, value_style="#58a6ff")
    if target == "serverless":
        print_kv("部署模式", artifact_type)
        print_kv("可观测性", "开启" if observability else "关闭")
    if account_id:
        print_kv("账号 ID", account_id)

    # 1. 检测框架
    detector = FrameworkDetector(str(agent_path))
    detection_result = detector.detect()

    if detection_result.type.value == "unknown":
        print_error("错误: 未检测到支持的框架")
        raise SystemExit(1)

    print_kv("框架", detection_result.name, value_style="#2da44e")

    # 2. 加载配置
    config = _load_config(agent_path)

    # 3. 确定部署名称
    deploy_name = name or config.get("name") or agent_path.name.replace("-", "_").replace(".", "_")
    print_kv("部署名称", deploy_name)

    # 4. 获取 Provider
    try:
        provider = DeploymentManager.get_provider(target)
    except ValueError as e:
        print_error(f"错误: {e}")
        print_info("使用 --list-providers 查看可用目标")
        raise SystemExit(1)

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
            "artifact_type": artifact_type,
            "ks3_path": ks3_path,
            "ks3_bucket": ks3_bucket,
            "image": image,
            "enable_observability": observability,
            "dry_run": dry_run,
            "no_cache": no_cache,
        },
    )

    # 更新资源配置
    if "resources" in config:
        deploy_target.resources.cpu = config["resources"].get("cpu", "2")
        deploy_target.resources.memory = config["resources"].get("memory", "4Gi")

    if "scaling" in config:
        deploy_target.scaling.min_replicas = config["scaling"].get("min_replicas", 1)
        deploy_target.scaling.max_replicas = config["scaling"].get("max_replicas", 10)
        deploy_target.scaling.concurrency = config["scaling"].get("concurrency", 10)

    # 6. 验证配置
    valid, error_msg = await provider.validate_config(deploy_target)
    if not valid:
        print_error(f"错误: 配置验证失败: {error_msg}")
        raise SystemExit(1)

    # 7. 打包 (Package 步骤仍需保留以获取框架信息等，但不构建制品)
    print_rule("Step 1/2 准备配置")
    try:
        package_info = await provider.package(str(agent_path), detection_result, config)
        package_info.name = deploy_name

        # 如果传入了 image 或 ks3_path，更新到 package_info
        if image:
            package_info.image = image
        if ks3_path:
            package_info.metadata["ks3_path"] = ks3_path

        print_kv("构建目录", str(package_info.build_dir))
        print_kv("框架", package_info.framework)
    except Exception as e:
        print_error(f"错误: 打包失败: {e}")
        raise SystemExit(1)

    # Dry Run 模式将由 Provider 内部处理 (通过 AgentEngineClient)


    # 8. 部署
    print_rule(f"Step 2/2 部署到 {target}")
    try:
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
                print_kv("APIKey", result.api_key, value_style="#d29922")
                # 首次部署提示 API Key 仅显示一次
                if result.message and "首次部署" in result.message:
                    print_warn("⚠️  API Key 仅在首次部署时明文显示，请妥善保存！")
            if result.message:
                print_kv("信息", result.message)
            
            # 9. 自动创建版本快照 (仅热更新时，首次部署平台自动创建 v1)
            is_update = result.message and "已更新" in result.message
            if result.agent_id and is_update and not no_version and not dry_run:
                from ksadk.cli.deploy_utils import auto_release_version
                await auto_release_version(result.agent_id, region, deploy_name)

            target_ref = result.agent_id or deploy_name
            print_next_steps([
                f"agentengine status --agent {target_ref}",
                f"agentengine invoke --agent {target_ref}",
            ])
        else:
            # 可能是 DryRun 的 SKIPPED
            if result.status.name == "SKIPPED":
                print_warn(f"部署状态: {result.status.value}")
            else:
                print_error(f"部署状态: {result.status.value}")
            if result.message:
                print_info(result.message)
            
            # 10. 部署失败时自动回滚 (如果启用了 --auto-rollback)
            if auto_rollback and result.agent_id and result.status.name not in ["SKIPPED"]:
                from ksadk.cli.deploy_utils import auto_rollback_to_previous
                await auto_rollback_to_previous(result.agent_id, region)
                
    except Exception as e:
        print_error(f"错误: 部署失败: {e}")
        import traceback

        traceback.print_exc()
        
        # 部署异常时也尝试回滚 (如果启用了 --auto-rollback，需要先获取 agent_id)
        # 由于异常时可能没有 result，这里暂不处理，留待后续优化
        
        raise SystemExit(1)





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
