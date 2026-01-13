"""
agentengin launch - 一键完成构建和部署
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
    type=click.Choice(["docker", "serverless"]),
    default="docker",
    help="部署目标 (default: docker)",
)
@click.option("--name", "-n", help="部署名称")
@click.option("--region", "-r", default="cn-beijing-6", help="区域 (serverless)")
@click.option("--account-id", envvar="KSYUN_ACCOUNT_ID", help="金山云账号 ID")
@click.option("--observability/--no-observability", default=True, help="是否启用可观测性")
def launch(
    agent_dir: str, target: str, name: str, region: str, account_id: str, observability: bool
):
    """一键完成构建和部署 (Build + Deploy)

    \b
    此命令会自动执行:
    1. 代码打包 / 镜像构建
    2. 上传代码到 KS3 / 推送镜像到 KCR
    3. 调用 API 创建或更新 Agent

    示例:
        agentengin launch .
        agentengin launch . --target serverless
    """
    asyncio.run(_launch_async(agent_dir, target, name, region, account_id, observability))


async def _launch_async(
    agent_dir: str, target: str, name: str, region: str, account_id: str, observability: bool
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
            # Launch 默认行为
            "artifact_type": "Code" if target == "serverless" else None,
            "ks3_bucket": "agentengin",
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

    # 6. 打包 (Package)
    click.secho("\n📦 Step 1/3: 准备构建环境...", fg="cyan", bold=True)
    try:
        package_info = await provider.package(str(agent_path), detection_result, config)
        package_info.name = deploy_name
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
            click.echo(f"   KS3 路径: {package_info.metadata.get('ks3_path')}")
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
            if result.message:
                click.echo(f"   信息:     {result.message}")

            click.echo("\n下一步:")
            click.echo(f"  agentengin status --agent {result.agent_name}")
            click.echo(f"  agentengin invoke --agent {result.agent_name}")
        else:
            click.secho(f"\n❌ 部署状态: {result.status.value}", fg="yellow")
            if result.message:
                click.echo(f"   {result.message}")

    except Exception as e:
        click.secho(f"❌ 部署异常: {e}", fg="red")
        import traceback

        traceback.print_exc()


def _load_config(agent_path: Path) -> dict:
    """加载配置文件"""
    import yaml

    config_path = agent_path / "agentengin.yaml"
    if not config_path.exists():
        config_path = agent_path / "ksadk.yaml"

    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f) or {}

    return {}
