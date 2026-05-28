"""
agentengine build - 构建 Agent 应用

支持两种模式:
- code: 打包 zip + 依赖 → 上传 KS3 (默认)
- container: 构建 Docker 镜像
"""

import asyncio
import click
import time
from datetime import datetime
from pathlib import Path
from ksadk.cli.error_utils import abort_with_cli_error, is_debug_mode_enabled, remote_error, validation_error
from ksadk.cli.workflow_common import print_workflow_header, render_workflow_result
from ksadk.cli.ui import (
    capture_standard_output,
    is_json_output,
    output_option as cli_output_option,
    print_error,
    print_info,
    print_kv,
    print_next_steps,
    print_rule,
    print_success,
    print_warn,
)


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
@click.option("--no-cache", is_flag=True, help="强制重新构建，不使用缓存 (code: 忽略已有 zip；container: docker --no-cache)")
@click.option("--repackage", is_flag=True, help="Code 模式复用依赖缓存，但强制重新打包当前代码/runtime")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="KS3 区域 (code 模式)")
@click.option("--ks3-bucket", help="KS3 bucket 名称 (code 模式, 默认: agentengine-{region})")
@cli_output_option()
def build(
    agent_dir: str,
    mode: str,
    tag: str,
    registry: str,
    push: bool,
    no_cache: bool,
    repackage: bool,
    region: str,
    ks3_bucket: str,
    output_mode: str | None,
):
    """将 Agent 应用构建为可部署的格式

    \b
    AGENT_DIR: Agent 项目目录 (默认: 当前目录)

    \b
    模式:
        code:      打包 zip + 依赖，上传 KS3 (默认)
        container: 构建 Docker 镜像

    \b
    示例:
        # 1) 默认构建 (code 模式)
        agentengine build .
        # 2) 显式指定构建参数
        agentengine build . --mode container --push --registry ghcr.io/your-org
        # 3) 显式指定区域
        KSYUN_REGION=cn-beijing-6 agentengine build . --mode code --push --no-cache
    """
    _ = output_mode
    agent_path = Path(agent_dir).resolve()
    try:
        with capture_standard_output():
            print_workflow_header(
                title="Agent 构建",
                subtitle=f"mode: {mode}",
                project_dir=agent_path,
                target="build",
                region=region if mode == "code" else None,
                mode_label="构建模式",
                mode_value=mode,
            )

            if mode == "container":
                result = _build_container(agent_path, tag, registry, push, no_cache)
            else:
                result = asyncio.run(_build_code(agent_path, push, region, ks3_bucket, no_cache, repackage))
    except Exception as e:
        if is_debug_mode_enabled():
            raise
        abort_with_cli_error(e, context="构建失败")
        return

    if is_json_output():
        render_workflow_result(action="build", result=result)


def _build_container(agent_path: Path, tag: str, registry: str, push: bool, no_cache: bool):
    """Container 模式构建"""
    from ksadk.builders import ContainerBuilder

    builder = ContainerBuilder(
        project_dir=agent_path, tag=tag, registry=registry, no_cache=no_cache
    )

    result = builder.build()

    if not result.success:
        raise validation_error(result.error_message or "构建失败")

    # 推送镜像
    if push and result.metadata.get("image"):
        if not builder.push(result.metadata["image"]):
            raise remote_error(
                "镜像推送失败",
                details={"image": str(result.metadata.get("image") or "")},
                hints=["请检查镜像仓库凭证、网络连通性和目标 registry 配置。"],
            )

        print_next_steps(
            [f"agentengine deploy --target serverless --image {result.metadata['image']} --artifact-type Container"]
        )

    # 摘要
    _print_summary("Container", result)
    return {
        "framework": result.metadata.get("framework", "unknown"),
        "artifact_type": "container",
        "artifact_source": "built_and_pushed" if push and result.metadata.get("image") else "built",
        "artifact_reused": bool(result.metadata.get("reused", False)),
        "artifact_built": True,
        "artifact_reference": str(result.metadata.get("image") or ""),
        "image": str(result.metadata.get("image") or ""),
        "push": bool(push),
    }


async def _build_code(
    agent_path: Path,
    push: bool,
    region: str,
    ks3_bucket: str = None,
    no_cache: bool = False,
    repackage: bool = False,
):
    """Code 模式构建"""
    from ksadk.builders import CodeBuilder, KS3Uploader

    builder = CodeBuilder(project_dir=agent_path, config={"no_cache": no_cache, "repackage": repackage})
    result = builder.build()

    if not result.success:
        raise validation_error(result.error_message or "构建失败")

    agent_name = result.metadata.get("agent_name", agent_path.name)

    # 上传到 KS3
    ks3_public_url = None
    ks3_internal_url = None

    if push:
        print_rule("上传到 KS3")
        upload_started_at = time.monotonic()
        
        # 预发特殊逻辑: region 为 pre-online 时，资源上传到 cn-beijing-6
        upload_region = "cn-beijing-6" if region == "pre-online" else region
        
        if region == "pre-online":
            print_warn("预发环境: 资源将上传到 cn-beijing-6 region")
        
        uploader = KS3Uploader(region=upload_region, bucket=ks3_bucket)
        
        # 使用时间戳确保每次上传的代码包路径唯一，支持真正的版本回滚
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        object_key = f"agents/{agent_name}/code_{timestamp}.zip"
        ks3_path = await uploader.upload(result.artifact_path, object_key)

        if ks3_path:
            print_success(f"上传成功: {ks3_path}")
            print_kv("上传耗时", _format_elapsed(upload_started_at))
            # 更新 metadata 以便持久化
            result.metadata["ks3_path"] = ks3_path
            result.metadata["pushed"] = True
            
            ks3_public_url = uploader.get_public_url_by_key(object_key)
            ks3_internal_url = uploader.get_internal_url_by_key(object_key)
            print_kv("公网地址", ks3_public_url or "-")
            print_kv("内网地址", ks3_internal_url or "-")
            print_info("回滚请使用历史不可变包路径 (ks3_path)")
            print_next_steps(["agentengine deploy --target serverless"])
        else:
            print_warn("上传失败，请检查 KS3 配置")
    else:
        print_info("跳过上传 (使用 --push 上传)")

    # 持久化构建元数据 (供 deploy 命令使用)
    metadata_file = agent_path / ".agentengine" / "build-metadata.json"
    try:
        import json
        from dataclasses import asdict
        
        # 自定义序列化: 处理 Path 对象
        def default_serializer(obj):
            if isinstance(obj, Path):
                return str(obj)
            raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

        data = asdict(result)
        # 确保目录存在
        metadata_file.parent.mkdir(parents=True, exist_ok=True)
        with open(metadata_file, "w") as f:
            json.dump(data, f, indent=2, default=default_serializer)
    except Exception as e:
        print_warn(f"无法保存构建元数据: {e}")

    # 摘要
    # show_next_step=True 意味着如果要引导用户做下一步操作的话。
    # 如果当前没有 push (push=False)，那么下一步通常引导去 push。
    _print_summary("Code", result, show_next_step=not push)
    return {
        "framework": result.metadata.get("framework", "unknown"),
        "artifact_type": "code",
        "artifact_source": "built_and_uploaded" if push and result.metadata.get("ks3_path") else "built",
        "artifact_reused": bool(result.metadata.get("reused", False)),
        "artifact_built": True,
        "artifact_reference": str(result.metadata.get("ks3_path") or result.artifact_path or ""),
        "artifact_path": str(result.artifact_path or ""),
        "artifact_size_mb": float(result.artifact_size_mb or 0),
        "ks3_path": str(result.metadata.get("ks3_path") or ""),
        "ks3_public_url": str(ks3_public_url or ""),
        "ks3_internal_url": str(ks3_internal_url or ""),
        "push": bool(push),
    }


def _format_elapsed(started_at: float) -> str:
    elapsed = max(0.0, time.monotonic() - started_at)
    if elapsed < 60:
        return f"{elapsed:.1f}s"
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)
    return f"{minutes}m{seconds:02d}s"


def _print_summary(mode: str, result, show_next_step: bool = False):
    """打印构建摘要"""
    print_rule(f"构建摘要 ({mode} 模式)")

    if result.artifact_path:
        print_kv("zip 文件", str(result.artifact_path))
        print_kv("大小", f"{result.artifact_size_mb:.2f} MB")

    if result.metadata.get("image"):
        print_kv("镜像", result.metadata["image"])

    print_kv("框架", result.metadata.get("framework", "unknown"))

    if mode == "Code":
        print_kv("依赖", "Linux x86_64 (via pip --platform)")

    if show_next_step and not result.metadata.get("pushed"):
        print_next_steps(["agentengine build --push"])
