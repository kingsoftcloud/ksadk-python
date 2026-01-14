"""
agentengine build - 构建 Agent 应用

支持两种模式:
- code: 打包 zip + 依赖 → 上传 KS3 (默认)
- container: 构建 Docker 镜像
"""

import asyncio
import click
from pathlib import Path


@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.argument("agent_dir", default=".", type=click.Path(exists=True))
@click.option(
    "--mode",
    "-m",
    type=click.Choice(["container", "code"]),
    default="code",
    help="构建模式: code (默认, zip+KS3) 或 container (Docker)",
)
@click.option("--tag", "-t", help="镜像标签 (container 模式)")
@click.option("--registry", help="镜像仓库地址 (container 模式)")
@click.option("--push", is_flag=True, help="构建后推送 (镜像到仓库 / zip到KS3)")
@click.option("--no-cache", is_flag=True, help="不使用缓存 (container 模式)")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="KS3 区域 (code 模式)")
@click.option("--ks3-bucket", help="KS3 bucket 名称 (code 模式, 默认: agentengine-{region})")
def build(
    agent_dir: str, mode: str, tag: str, registry: str, push: bool, no_cache: bool, region: str, ks3_bucket: str
):
    """将 Agent 应用构建为可部署的格式

    \b
    AGENT_DIR: Agent 项目目录 (默认: 当前目录)

    模式:
        code:      打包 zip + 依赖，上传 KS3 (默认)
        container: 构建 Docker 镜像

    示例:
        agentengine build .
        agentengine build . --mode code --push
        agentengine build . --mode container --push --registry kcr.cn-beijing-6.ksyuncs.com
    """
    agent_path = Path(agent_dir).resolve()
    click.echo(f"📁 项目目录: {agent_path}")
    click.echo(f"🔧 构建模式: {click.style(mode, fg='cyan')}")

    if mode == "container":
        _build_container(agent_path, tag, registry, push, no_cache)
    else:
        asyncio.run(_build_code(agent_path, push, region, ks3_bucket))


def _build_container(agent_path: Path, tag: str, registry: str, push: bool, no_cache: bool):
    """Container 模式构建"""
    from ksadk.builders import ContainerBuilder

    builder = ContainerBuilder(
        project_dir=agent_path, tag=tag, registry=registry, no_cache=no_cache
    )

    result = builder.build()

    if not result.success:
        click.secho(f"❌ {result.error_message}", fg="red")
        raise SystemExit(1)

    # 推送镜像
    if push and result.metadata.get("image"):
        if not builder.push(result.metadata["image"]):
            raise SystemExit(1)

        click.echo("")
        click.echo("下一步:")
        click.echo(
            f"  agentengine deploy --target serverless --image {result.metadata['image']} --artifact-type Container"
        )

    # 摘要
    _print_summary("Container", result)


async def _build_code(agent_path: Path, push: bool, region: str, ks3_bucket: str = None):
    """Code 模式构建"""
    from ksadk.builders import CodeBuilder, KS3Uploader

    builder = CodeBuilder(project_dir=agent_path)
    result = builder.build()

    if not result.success:
        click.secho(f"❌ {result.error_message}", fg="red")
        raise SystemExit(1)

    agent_name = result.metadata.get("agent_name", agent_path.name)

    # 上传到 KS3
    ks3_public_url = None
    ks3_internal_url = None

    if push:
        click.echo("\n📤 上传到 KS3...")
        
        # 预发特殊逻辑: region 为 pre-online 时，资源上传到 cn-beijing-6
        upload_region = "cn-beijing-6" if region == "pre-online" else region
        
        if region == "pre-online":
            click.echo(f"   ⚠️  预发环境: 资源将上传到 cn-beijing-6 region")
        
        uploader = KS3Uploader(region=upload_region, bucket=ks3_bucket)
        object_key = f"agents/{agent_name}/code.zip"
        ks3_path = await uploader.upload(result.artifact_path, object_key)

        if ks3_path:
            click.secho(f"   ✅ 上传成功: {ks3_path}", fg="green")
            ks3_public_url = uploader.get_public_url(agent_name)
            ks3_internal_url = uploader.get_internal_url(agent_name)
            click.echo(f"   📎 公网地址: {ks3_public_url}")
            click.echo(f"   📎 内网地址: {ks3_internal_url}")

            click.echo("")
            click.echo("下一步:")
            click.echo(f"  agentengine deploy --target serverless --ks3-path {ks3_path}")
        else:
            click.secho("   ⚠️  上传失败，请检查 KS3 配置", fg="yellow")
    else:
        click.echo("\n⏭️  跳过上传 (使用 --push 上传)")

    # 摘要
    _print_summary("Code", result, push)


def _print_summary(mode: str, result, show_next_step: bool = False):
    """打印构建摘要"""
    click.echo("\n" + "─" * 50)
    click.echo(f"📊 构建摘要 ({mode} 模式):")

    if result.artifact_path:
        click.echo(f"   zip 文件: {result.artifact_path}")
        click.echo(f"   大小: {result.artifact_size_mb:.2f} MB")

    if result.metadata.get("image"):
        click.echo(f"   镜像: {result.metadata['image']}")

    click.echo(f"   框架: {result.metadata.get('framework', 'unknown')}")

    if mode == "Code":
        click.echo(f"   依赖: Linux x86_64 (via pip --platform)")

    if show_next_step and not result.metadata.get("pushed"):
        click.echo("")
        click.echo("下一步: agentengine build --push")
