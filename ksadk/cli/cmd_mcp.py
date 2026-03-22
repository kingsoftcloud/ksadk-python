"""agentengine mcp - MCP 资源管理。"""

import os
import click
from datetime import datetime
from pathlib import Path
from ksadk.api.client import DryRunExit
from ksadk.cli.agent_ref import resolve_mcp_ref
from ksadk.cli.dry_run import dry_run_option, run_async_with_dry_run, effective_dry_run
from ksadk.cli.error_utils import abort_with_cli_error, remote_error, resolution_error, usage_error, validation_error
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
    try:
        result = run_async_with_dry_run(
            _deploy_mcp_async(mcp_dir, name, region, ks3_bucket, enable_auth, dry_run, artifact_type, no_cache),
            dry_run=dry_run,
            dry_run_resource="mcp",
            dry_run_action="deploy",
        )
    except Exception as e:
        _abort_mcp_error(e, context="部署失败", argv=["mcp", "deploy"])
        return
    if result is not None and is_json_output():
        render_descriptor_status(
            MCP_RESOURCE,
            title="MCP 部署结果",
            subtitle=str(result.get("name") or result.get("id") or "-"),
            fields=[
                ("ID", str(result.get("id") or "-"), None),
                ("名称", str(result.get("name") or "-"), None),
                ("状态", str(result.get("status") or "DEPLOYED"), None),
                ("Endpoint", str(result.get("endpoint") or "-"), "#58a6ff"),
                ("MCP URL", str(result.get("mcp_url") or "-"), "#58a6ff"),
            ],
            action="deploy",
            item=result,
        )


async def _deploy_mcp_async(
    mcp_dir: str,
    name: str,
    region: str,
    ks3_bucket: str,
    enable_auth: bool,
    dry_run: bool,
    artifact_type: str,
    no_cache: bool = False,
):
    """异步 MCP 部署流程"""
    from ksadk.detection.mcp_detector import MCPDetector
    from ksadk.api import AgentEngineClient
    
    mcp_path = Path(mcp_dir).resolve()
    from ksadk.deployment.state import load_state, save_state
    print_title("MCP 部署", f"region: {region}")
    print_kv("项目目录", str(mcp_path))
    
    # 1. 检测 MCP 项目
    detector = MCPDetector(str(mcp_path))
    detection_result = detector.detect()
    
    if not detection_result.is_valid:
        raise validation_error(
            "未检测到 FastMCP 项目。",
            hints=["请确保项目包含 `from fastmcp import FastMCP`。"],
        )
    
    print_success("检测到 FastMCP 项目")
    print_kv("入口", detection_result.entry_point)
    print_kv("MCP 变量", detection_result.mcp_variable)
    if detection_result.tools:
        print_kv("工具", ", ".join(detection_result.tools))
    
    mcp_name = name or mcp_path.name.replace('-', '_').replace('.', '_')
    
    artifact_path_final = None
    upload_region = "cn-beijing-6" if region == "pre-online" else region

    if artifact_type.lower() == "code":
        # 2. 构建代码包
        print_rule(f"构建代码包: {mcp_name} (Code)")
        from ksadk.builders.mcp_builder import MCPCodeBuilder
        builder = MCPCodeBuilder(mcp_path, config={"no_cache": no_cache})
        with capture_standard_output():
            build_result = builder.build()
        
        if not build_result.success:
            raise validation_error(build_result.error_message or "构建失败")
        
        # 3. 上传到 KS3
        print_rule("上传到 KS3")
        from ksadk.builders.ks3_uploader import KS3Uploader
        # 让 KS3Uploader 使用默认 bucket 逻辑 (ACCOUNT_ID)
        uploader = KS3Uploader(region=upload_region, bucket=ks3_bucket)
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        object_key = f"mcps/{mcp_name}/code_{timestamp}.zip"
        
        with capture_standard_output():
            ks3_path = await uploader.upload(build_result.artifact_path, object_key)
        
        if not ks3_path:
            raise remote_error("KS3 上传失败")
        
        print_success(f"上传成功: {ks3_path}")
        artifact_path_final = ks3_path
        
    elif artifact_type.lower() == "container":
        # 2. 构建 MCP Docker 镜像 (复用 ContainerBuilder，和 agent container 模式一致)
        print_rule(f"构建 Docker 镜像: {mcp_name}")
        
        # 检查 Docker 是否可用
        import subprocess
        try:
            docker_check = subprocess.run(["docker", "version"], capture_output=True, timeout=10)
            if docker_check.returncode != 0:
                raise validation_error("Docker 未正常运行")
        except FileNotFoundError:
            raise validation_error("未找到 docker 命令，请先安装 Docker")
        
        from ksadk.builders.container_builder import ContainerBuilder
        container_builder = ContainerBuilder(
            project_dir=mcp_path,
            no_cache=no_cache,
        )
        # 利用 ContainerBuilder 内置的 build()，它会自动检测项目、确定镜像名和推送
        with capture_standard_output():
            build_result = container_builder.build()
        
        if not build_result.success:
            raise validation_error(f"Docker 镜像构建失败: {build_result.error_message}")
        
        image_name = build_result.metadata.get("image")
        print_success(f"镜像构建成功: {image_name}")
        
        # 3. 推送镜像
        print_rule("推送镜像到仓库")
        with capture_standard_output():
            pushed = container_builder.push(image_name)
        if not pushed:
            raise remote_error("镜像推送失败")
        
        print_success(f"推送成功: {image_name}")
        artifact_path_final = image_name
    else:
        raise usage_error(f"不支持的 artifact_type: {artifact_type}")

    # 4. 读取本地状态文件
    state = load_state(mcp_path)
    existing_mcp_id = None
    
    if state.get("type") == "mcp":
        existing_mcp_id = state.get("mcp_id")
    
    # 5. 调用 Server API (使用 AgentEngineClient 默认内网地址)
    print_rule("部署 MCP Server")
    
    from ksadk.common.auth import AWSV4Auth
    auth = AWSV4Auth()
    
    request_data = {
        "name": mcp_name,
        "type": "mcp",
        "artifact_type": artifact_type,
        "artifact_path": artifact_path_final,
        "region": region,
        "enable_auth": enable_auth,
        "resources": {"cpu": "1", "memory": "2Gi"},
        "scaling": {"min_replicas": 1, "max_replicas": 5, "concurrency": 20},
        "metadata": {
            "mcp_variable": detection_result.mcp_variable,
            "tools": detection_result.tools,
        }
    }
    
    if auth.access_key_id and auth.secret_access_key:
        if artifact_type.lower() == "code":
            # Code 模式: 传递 KS3 凭证，让 Server 能从 KS3 拉取代码包
            request_data["ks3"] = {
                "access_key": auth.access_key_id,
                "secret_key": auth.secret_access_key,
                "region": upload_region,
                "bucket": uploader.bucket_name,
            }
    
    if artifact_type.lower() == "container":
        # Container 模式: 传递 KCR 镜像凭证，让 Server 能拉取私有镜像
        kcr_username = os.getenv("KCR_USERNAME", "") or os.getenv("KSYUN_ACCOUNT_ID", "")
        kcr_password = os.getenv("KCR_PASSWORD")
        kcr_endpoint = os.getenv("KCR_ENDPOINT", "hub.kce.ksyun.com")

        if kcr_username and kcr_password:
            request_data["image_credential"] = {
                "endpoint": kcr_endpoint,
                "username": kcr_username,
                "password": kcr_password,
            }
            print_kv("镜像凭证", f"{kcr_username}@{kcr_endpoint}")
        else:
            print_warn("未配置镜像凭证 (KCR_USERNAME/KCR_PASSWORD)，私有镜像可能无法拉取")

    if dry_run:
        async with AgentEngineClient(region=region, dry_run=True) as client:
            if existing_mcp_id:
                await client.update_mcp(existing_mcp_id, request_data)
            else:
                await client.create_mcp(request_data)
        return

    async with AgentEngineClient(region=region) as client:
        if existing_mcp_id:
            # 更新
            print_info(f"检测到本地状态: {existing_mcp_id}")
            print_info("执行热更新...")
            res = await client.update_mcp(existing_mcp_id, request_data)
            mcp_id = existing_mcp_id
        else:
            # 创建
            res = await client.create_mcp(request_data)
            if not res:
                raise remote_error("Server 返回空响应，请检查 MCP 名称是否冲突或服务端日志。")
            mcp_id = res.get("mcp_id")

        endpoint = res.get("endpoint")

        # 保存本地状态
        state_data = {
            "type": "mcp",
            "mcp_id": mcp_id,
            "name": mcp_name,
            "region": region,
            "artifact_type": artifact_type,
            "endpoint": endpoint,
            "mcp_endpoint": f"{endpoint}/mcp" if endpoint else None,
            "tools": detection_result.tools,
        }
        if res.get("api_key"):
            state_data["api_key"] = res.get("api_key")

        save_state(mcp_path, state_data)

        print_info("已保存状态到 .agentengine.state")

        # 输出结果
        print_success("MCP 部署成功")
        print_kv("MCP ID", mcp_id)
        print_kv("模式", artifact_type)
        if endpoint:
            print_kv("Endpoint", endpoint, value_style="#58a6ff")
            print_kv("MCP URL", f"{endpoint}/mcp", value_style="#58a6ff")
        print_rule("调用方式")
        print_info('# Cursor/Claude: {"url": "<endpoint>/mcp"}')
        print_info('LangChain/LangGraph: MCPClientToolkit(url="<endpoint>/mcp")')
        print_info('ADK: MCPToolset.from_server(connection_params={"url": "<endpoint>/mcp"})')
        return {
            "id": str(mcp_id or ""),
            "name": str(mcp_name),
            "status": "DEPLOYED",
            "region": region,
            "artifact_type": artifact_type,
            "artifact_reference": str(artifact_path_final or ""),
            "endpoint": str(endpoint or ""),
            "mcp_url": str(f"{endpoint}/mcp" if endpoint else ""),
            "api_key_present": bool(res.get("api_key")),
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
        render_descriptor_status(
            MCP_RESOURCE,
            title="MCP 删除结果",
            subtitle=", ".join(result["targets"]) if result["targets"] else "-",
            fields=[
                ("目标数量", str(len(result["targets"])), None),
                ("已删除", ", ".join(result["deleted"]) or "-", None),
                ("失败", ", ".join(result["failed"]) or "-", None),
            ],
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
