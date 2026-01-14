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


@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.argument("agent_dir", default=".", type=click.Path(exists=True))
@click.option(
    "--target",
    "-t",
    type=click.Choice(["docker", "k8s", "serverless"]),
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
    dry_run: bool,
    list_providers: bool,
):
    """部署 Agent 到云端

    \b
    AGENT_DIR: Agent 项目目录 (默认: 当前目录)

    示例:
        agentengine deploy .                              # Serverless (默认)
        agentengine deploy . --target docker              # 本地 Docker
        agentengine deploy . --target k8s                 # K8s 集群
        agentengine deploy . --artifact-type Container    # Serverless 容器模式
        agentengine deploy . --dry-run                    # 打印 curl 请求
        agentengine deploy . --region cn-shanghai-1
        agentengine deploy . --account-id 2000003485
    """
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
            dry_run,
        )
    )


def _list_providers():
    """列出所有可用的部署 Provider"""
    from ksadk.deployment import DeploymentManager

    providers = DeploymentManager.list_providers()

    click.secho("可用的部署目标:", fg="blue", bold=True)
    click.echo("")

    for p in providers:
        features = []
        if p.get("supports_scaling"):
            features.append("扩缩容")
        if p.get("supports_streaming"):
            features.append("流式")
        if p.get("requires_image_registry"):
            features.append("需镜像仓库")

        click.echo(f"  {click.style(p['name'], fg='green', bold=True)}")
        click.echo(f"    {p.get('display_name', p['name'])}")
        if p.get("description"):
            click.echo(f"    {p['description']}")
        if features:
            click.echo(f"    特性: {', '.join(features)}")
        click.echo("")


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
    dry_run: bool,
):
    """异步部署流程"""
    from ksadk.detection import FrameworkDetector
    from ksadk.deployment import DeploymentManager, DeployTarget, DeployStatus

    agent_path = Path(agent_dir).resolve()
    click.echo(f"项目目录: {agent_path}")
    click.echo(f"部署目标: {click.style(target, fg='cyan')}")
    click.echo(f"区域: {region}")
    if target == "serverless":
        click.echo(f"部署模式: {artifact_type}")
        click.echo(f"可观测性: {'开启' if observability else '关闭'}")
    if account_id:
        click.echo(f"账号 ID: {account_id}")

    # 1. 检测框架
    detector = FrameworkDetector(str(agent_path))
    detection_result = detector.detect()

    if detection_result.type.value == "unknown":
        click.secho("错误: 未检测到支持的框架", fg="red")
        raise SystemExit(1)

    click.echo(f"框架: {click.style(detection_result.name, fg='green')}")

    # 2. 加载配置
    config = _load_config(agent_path)

    # 3. 确定部署名称
    deploy_name = name or config.get("name") or agent_path.name.replace("-", "_").replace(".", "_")
    click.echo(f"部署名称: {deploy_name}")

    # 4. 获取 Provider
    try:
        provider = DeploymentManager.get_provider(target)
    except ValueError as e:
        click.secho(f"错误: {e}", fg="red")
        click.echo("使用 --list-providers 查看可用目标")
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
        click.secho(f"错误: 配置验证失败: {error_msg}", fg="red")
        raise SystemExit(1)

    # 7. 打包 (Package 步骤仍需保留以获取框架信息等，但不构建制品)
    click.echo("\nStep 1/2: 准备配置...")
    try:
        package_info = await provider.package(str(agent_path), detection_result, config)
        package_info.name = deploy_name

        # 如果传入了 image 或 ks3_path，更新到 package_info
        if image:
            package_info.image = image
        if ks3_path:
            package_info.metadata["ks3_path"] = ks3_path

        click.echo(f"   构建目录: {package_info.build_dir}")
        click.echo(f"   框架: {package_info.framework}")
    except Exception as e:
        click.secho(f"错误: 打包失败: {e}", fg="red")
        raise SystemExit(1)

    # Dry Run 模式 - 打印 curl 请求
    if dry_run:
        click.echo("\n" + "=" * 60)
        click.secho("Dry Run 模式 - 只生成配置，不执行部署", fg="yellow", bold=True)
        click.echo("=" * 60)

        if target == "serverless":
            _print_serverless_curl(package_info, deploy_target, account_id, region)
        else:
            click.echo(f"\n生成的配置:")
            click.echo(f"   Dockerfile: {package_info.dockerfile}")
            click.echo(f"   构建目录: {package_info.build_dir}")
        return

    # 9. 部署
    click.echo(f"\nStep 2/2: 部署到 {target}...")
    try:
        result = await provider.deploy(package_info, deploy_target)

        if result.is_success():
            click.echo("")
            click.secho("部署成功!", fg="green", bold=True)
            click.echo("-" * 50)
            click.echo(f"   名称:     {result.agent_name or deploy_name}")
            click.echo(f"   状态:     {result.status.value}")
            if result.endpoint:
                click.echo(f"   Endpoint: {result.endpoint}")
            if result.api_key:
                click.echo(f"   APIKey:   {result.api_key}")
            if result.message:
                click.echo(f"   信息:     {result.message}")

            click.echo("")
            click.echo("下一步:")
            click.echo(f"  agentengine status --agent {deploy_name}")
            click.echo(f"  agentengine invoke --agent {deploy_name}")
        else:
            click.secho(f"\n部署状态: {result.status.value}", fg="yellow")
            if result.message:
                click.echo(f"   {result.message}")
    except Exception as e:
        click.secho(f"错误: 部署失败: {e}", fg="red")
        import traceback

        traceback.print_exc()
        raise SystemExit(1)


def _print_serverless_curl(package_info, deploy_target, account_id: str, region: str):
    """打印 Serverless API 的 curl 请求"""
    import uuid

    # 构建请求体 (与 ServerlessProvider._build_create_input 一致)
    artifact_type = deploy_target.extra.get("artifact_type", "Container")

    # 解析资源
    cpu_str = deploy_target.resources.cpu
    if "m" in cpu_str:
        cpu = float(cpu_str.replace("m", "")) / 1000
    else:
        cpu = float(cpu_str)

    memory_str = deploy_target.resources.memory
    if "Gi" in memory_str:
        memory = int(memory_str.replace("Gi", ""))
    elif "Mi" in memory_str:
        # 向上取整，如 512Mi -> 1G
        mi_val = int(memory_str.replace("Mi", ""))
        memory = max(1, (mi_val + 1023) // 1024)
    else:
        memory = int(memory_str)

    request_body = {
        "agentRuntimeName": package_info.name,
        "artifactType": artifact_type,
        "description": f"{package_info.framework} Agent",
        "cpu": cpu,
        "memory": memory,
        "port": deploy_target.extra.get("port", 8000),
        "instanceCount": deploy_target.scaling.min_replicas,
        "sessionConcurrencyLimitPerInstance": deploy_target.scaling.concurrency,
    }

    # Container 模式
    if artifact_type == "Container":
        request_body["containerConfiguration"] = {
            "image": package_info.image or f"agentengine/{package_info.name}:latest",
            "command": ["python", "entrypoint.py"],
        }

    # Code 模式
    else:
        ak = os.environ.get("KS3_ACCESS_KEY") or os.environ.get("KSYUN_ACCESS_KEY", "")
        sk = os.environ.get("KS3_SECRET_KEY") or os.environ.get("KSYUN_SECRET_KEY", "")
        ks3_path = package_info.metadata.get("ks3_path", "")

        if ks3_path.startswith("ks3://"):
            parts = ks3_path[6:].split("/", 1)
            bucket_name = parts[0]
            object_name = parts[1] if len(parts) > 1 else ""
        else:
            # 默认 Bucket 逻辑: agentengine-{region}
            # 注意: pre-online 使用 cn-beijing-6 的资源
            ks3_region = "cn-beijing-6" if region == "pre-online" else region
            bucket_name = f"agentengine-{ks3_region}"
            object_name = f"agents/{package_info.name}/code.zip"

        if os.environ.get("KS3_ENDPOINT"):
            ks3_endpoint = os.environ.get("KS3_ENDPOINT")
        else:
            _, internal_ep = get_ks3_endpoints(region)
            ks3_endpoint = internal_ep

        request_body["codeConfiguration"] = {
            "ks3BucketName": bucket_name,
            "ks3ObjectName": object_name,
            "ks3Region": region,
            "ks3AccessKey": ak[:8] + "****" if ak else "<KS3_ACCESS_KEY>",
            "ks3SecretKey": "****" if sk else "<KS3_SECRET_KEY>",
            "ks3Endpoint": ks3_endpoint,
            "language": "python",
            "command": [
                "python",
                "-m",
                "uvicorn",
                "entrypoint:app",
                "--host",
                "0.0.0.0",
                "--port",
                "8000",
            ],
        }

    # 模型配置 (如果有)
    model_name = os.environ.get("MODEL_NAME")
    model_api_base = os.environ.get("OPENAI_API_BASE") or os.environ.get("MODEL_API_BASE")
    model_api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("MODEL_API_KEY")

    if model_name and model_api_base and model_api_key:
        request_body["modelConfiguration"] = {
            "modelName": model_name,
            "modelApiBase": model_api_base,
            "modelApiKey": model_api_key[:8] + "****",
        }

    # Observability (Langfuse)
    enable_observability = deploy_target.extra.get("enable_observability", True)
    langfuse_public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key = os.environ.get("LANGFUSE_SECRET_KEY")

    if enable_observability and langfuse_public_key and langfuse_secret_key:
        request_body["observability"] = {
            "langfuse": {
                "enabled": True,
                "host": os.environ.get("LANGFUSE_HOST") or "http://localhost:3000",
                "publicKey": langfuse_public_key,
                "secretKey": langfuse_secret_key,
            }
        }

    # 打印信息
    endpoint = os.environ.get(
        "SERVERLESS_ENDPOINT",
        DEFAULT_SERVERLESS_ENDPOINT,
    )
    request_id = str(uuid.uuid4())

    click.echo(f"\n配置信息:")
    click.echo(f"   Endpoint:   {endpoint}")
    click.echo(f"   Account ID: {account_id or '<KSYUN_ACCOUNT_ID>'}")
    click.echo(f"   Region:     {region}")
    click.echo(f"   Agent Name: {package_info.name}")
    click.echo(f"   Artifact:   {artifact_type}")

    click.echo(f"\n请求体 (Request Body):")
    click.echo(json.dumps(request_body, indent=2, ensure_ascii=False))

    # 构建 curl 命令
    ak = os.environ.get("KSYUN_ACCESS_KEY") or os.environ.get("KS3_ACCESS_KEY", "")
    sk = os.environ.get("KSYUN_SECRET_KEY") or os.environ.get("KS3_SECRET_KEY", "")

    click.echo(f"\ncurl 命令 (需要 AWS V4 签名):")
    click.echo("-" * 60)

    body_json = json.dumps(request_body, ensure_ascii=False)

    curl_cmd = f'''curl -X POST "{endpoint}/CreateAgentRuntime" \\
  -H "Content-Type: application/json" \\
  -H "Accept: application/json" \\
  -H "X-Ksc-Request-Id: {request_id}" \\
  -H "X-Ksc-Account-Id: {account_id or "<KSYUN_ACCOUNT_ID>"}" \\
  -H "X-Ksc-Region: {region}" \\
  -d '{body_json}' '''

    click.echo(curl_cmd)

    click.echo("-" * 60)
    click.secho("\n注意: 实际请求需要 AWS V4 签名，上述 curl 仅供参考", fg="yellow")
    click.echo("环境变量:")
    click.echo(f"   KSYUN_ACCOUNT_ID = {account_id or '(未设置)'}")
    click.echo(f"   KSYUN_ACCESS_KEY = {ak[:8] + '****' if ak else '(未设置)'}")
    click.echo(f"   KSYUN_SECRET_KEY = {'****' if sk else '(未设置)'}")


def _load_config(agent_path: Path) -> dict:
    """加载配置文件"""
    import yaml

    # 优先 agentengine.yaml
    config_path = agent_path / "agentengine.yaml"
    if not config_path.exists():
        config_path = agent_path / "ksadk.yaml"

    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f) or {}

    return {}
