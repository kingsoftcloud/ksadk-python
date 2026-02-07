"""
agentengine launch - 一键完成构建和部署
"""

import click
import asyncio
import os
from pathlib import Path


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
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域 (serverless)")
@click.option("--account-id", envvar="KSYUN_ACCOUNT_ID", help="金山云账号 ID")
@click.option("--observability/--no-observability", default=True, help="是否启用可观测性")
@click.option("--no-cache", is_flag=True, help="强制重新构建，不使用缓存")
@click.option("--port", "-p", default=8000, help="服务端口 (default: 8000)")
@click.option("--namespace", default="default", help="K8s 命名空间")
@click.option("--registry", help="镜像仓库地址")
@click.option("--ks3-bucket", help="KS3 bucket 名称")
@click.option("--ks3-path", help="KS3 代码包路径")
@click.option("--image", help="Docker 镜像地址")
@click.option("--dry-run", is_flag=True, help="仅打印请求，不执行实际操作")
@click.option(
    "--artifact-type",
    type=click.Choice(["Code", "Container"]),
    help="部署模式 (serverless default: Code)",
)
@click.option("--no-version", is_flag=True, help="部署成功后不自动创建版本快照")
@click.option("--auto-rollback", is_flag=True, help="部署失败时自动回滚到上一版本")
def launch(
    agent_dir: str,
    target: str,
    name: str,
    region: str,
    account_id: str,
    observability: bool,
    no_cache: bool,
    port: int,
    namespace: str,
    registry: str,
    ks3_bucket: str,
    ks3_path: str,
    image: str,
    dry_run: bool,
    artifact_type: str,
    no_version: bool,
    auto_rollback: bool,
):
    """一键完成构建和部署 (Build + Deploy)

    \b
    此命令会自动执行:
    1. 代码打包 / 镜像构建
    2. 上传代码到 KS3 / 推送镜像到 KCR
    3. 调用 API 创建或更新 Agent

    示例:
        agentengine launch .                              # Serverless (默认, Code模式)
        agentengine launch . --no-cache                   # 强制重新构建
        agentengine launch . -t kcf                       # 部署到 KCF (云函数)
        agentengine launch . -t kce                       # 部署到 KCE (容器引擎)
    """
    asyncio.run(
        _launch_async(
            agent_dir,
            target,
            name,
            region,
            account_id,
            observability,
            no_cache,
            port,
            namespace,
            registry,
            ks3_bucket,
            ks3_path,
            image,
            dry_run,
            artifact_type,
            no_version,
            auto_rollback,
        )
    )


async def _launch_async(
    agent_dir: str,
    target: str,
    name: str,
    region: str,
    account_id: str,
    observability: bool,
    no_cache: bool,
    port: int,
    namespace: str,
    registry: str,
    ks3_bucket: str,
    ks3_path: str,
    image: str,
    dry_run: bool,
    artifact_type: str,
    no_version: bool,
    auto_rollback: bool,
):
    from ksadk.detection import FrameworkDetector
    from ksadk.deployment import DeploymentManager, DeployTarget

    agent_path = Path(agent_dir).resolve()
    click.secho("🚀 AgentEngine Launch", fg="blue", bold=True)
    click.echo("─" * 50)
    click.echo(f"📁 项目目录: {agent_path}")
    click.echo(f"🎯 部署目标: {target}")
    if target == "serverless":
        click.echo(f"🌍 区域: {region}")
        if not artifact_type:
            artifact_type = "Code"
        click.echo(f"📦 模式: {artifact_type}")
        if account_id:
            click.echo(f"👤 账号: {account_id}")

    # 1. 检测框架
    detector = FrameworkDetector(str(agent_path))
    detection_result = detector.detect()
    if detection_result.type.value == "unknown":
        click.secho("❌ 错误: 未检测到支持的框架", fg="red")
        return

    click.echo(f"📦 框架: {click.style(detection_result.name, fg='green')}")

    # 2. 加载配置
    config = _load_config(agent_path)
    deploy_name = name or config.get("name") or agent_path.name.replace("-", "_").replace(".", "_")
    click.echo(f"🏷️  部署名称: {deploy_name}")

    # 3. 获取 Provider
    try:
        provider = DeploymentManager.get_provider(target)
    except ValueError as e:
        click.secho(f"❌ 错误: {e}", fg="red")
        return

    # 4. 准备 Target 配置
    deploy_target = DeployTarget(
        provider=target,
        region=region,
        project_id=config.get("project_id", "default"),
        extra={
            "account_id": account_id,
            "enable_observability": observability,
            "no_cache": no_cache,
            "artifact_type": artifact_type,
            "port": port,
            "namespace": namespace,
            "registry": registry,
            "ks3_bucket": ks3_bucket,
            "ks3_path": ks3_path,
            "image": image,
            "dry_run": dry_run,
        },
    )

    # 资源配置
    if "resources" in config:
        deploy_target.resources.cpu = config["resources"].get("cpu", "2")
        deploy_target.resources.memory = config["resources"].get("memory", "4Gi")

    # 5. 验证
    valid, error_msg = await provider.validate_config(deploy_target)
    if not valid:
        click.secho(f"❌ 配置验证失败: {error_msg}", fg="red")
        return

    # 避免 package 阶段加载旧 metadata，如果在 no_cache 模式下，直接物理删除
    if no_cache:
        metadata_file = agent_path / ".agentengine" / "build-metadata.json"
        if metadata_file.exists():
            try:
                os.remove(metadata_file)
                click.secho(f"🗑️  [DEBUG] 已删除旧 build-metadata.json (--no-cache)", fg="yellow")
            except Exception:
                pass

    # 6. 打包 (Package)
    click.secho("\n📦 Step 1/3: 准备构建环境...", fg="cyan", bold=True)
    try:
        package_info = await provider.package(str(agent_path), detection_result, config)
        package_info.name = deploy_name
        
        # 传入额外的 metadata override
        if ks3_path:
            package_info.metadata["ks3_path"] = ks3_path
        
        click.echo(f"   构建目录: {package_info.build_dir}")
    except Exception as e:
        click.secho(f"❌ 打包失败: {e}", fg="red")
        return

    # 7. 构建与上传 (Build)
    click.secho("\n🔨 Step 2/3: 构建与上传...", fg="cyan", bold=True)
    try:
        # 这里会触发 Code 模式的 KS3 上传 或 Container 模式的 Docker Build
        package_info = await provider.build(package_info, deploy_target)

        if target == "serverless":
            # Serverless Provider 在 build 后会将 ks3_path 放入 metadata
            ks3 = package_info.metadata.get("ks3_path")
            if ks3:
                click.echo(f"   KS3 路径: {ks3}")
        else:
            click.echo(f"   镜像: {package_info.image}")

    except Exception as e:
        click.secho(f"❌ 构建失败: {e}", fg="red")
        return

    # 8. 部署 (Deploy)
    click.secho(f"\n🚀 Step 3/3: 部署到 {target}...", fg="cyan", bold=True)
    try:
        result = await provider.deploy(package_info, deploy_target)

        if result.is_success():
            click.echo("\n" + "─" * 50)
            click.secho("✅ 部署成功!", fg="green", bold=True)
            click.echo(f"   名称:     {result.agent_name}")
            click.echo(f"   状态:     {result.status.value}")
            if result.endpoint:
                click.echo(f"   Endpoint: {result.endpoint}")
            if result.api_key:
                click.secho(f"   API Key:  {result.api_key}", fg="yellow", bold=True)
                click.secho("   ⚠️  请妥善保存此 API Key，它仅在首次部署时显示！", fg="yellow")
            if result.message:
                click.echo(f"   信息:     {result.message}")
            
            # 9. 自动创建版本快照 (除非指定 --no-version)
            if result.agent_id and not no_version and not dry_run:
                from ksadk.cli.deploy_utils import auto_release_version
                await auto_release_version(result.agent_id, region, deploy_name)

            click.echo(f"\n下一步查看或使用{result.agent_name}:")
            click.echo(f"  agentengine status --agent {result.agent_id}")
            click.echo(f"  agentengine invoke --agent {result.agent_id}")
        else:
            click.secho(f"\n❌ 部署状态: {result.status.value}", fg="yellow")
            if result.message:
                click.echo(f"   {result.message}")
            
            # 10. 自动回滚
            if auto_rollback and result.agent_id and result.status.name not in ["SKIPPED"]:
                from ksadk.cli.deploy_utils import auto_rollback_to_previous
                await auto_rollback_to_previous(result.agent_id, region)

    except Exception as e:
        click.secho(f"❌ 部署异常: {e}", fg="red")
        import traceback

        traceback.print_exc()


def _load_config(agent_path: Path) -> dict:
    """加载配置文件"""
    import yaml

    config_path = agent_path / "agentengine.yaml"
    if not config_path.exists():
        config_path = agent_path / "ksadk.yaml"

    if config_path.exists():
        # 使用 utf-8-sig 自动处理 BOM，确保 Windows 兼容性
        with open(config_path, encoding='utf-8-sig') as f:
            return yaml.safe_load(f) or {}

    return {}
