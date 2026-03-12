"""
agentengine mcp - MCP Server 管理

支持操作:
- deploy: 部署 MCP Server 到云端
- list: 列出已部署的 MCP
- status: 查看 MCP 状态
- delete: 删除 MCP
"""

import os
import click
from pathlib import Path
from ksadk.api.client import DryRunExit
from ksadk.cli.dry_run import dry_run_option, run_async_with_dry_run, effective_dry_run
from ksadk.cli.error_utils import print_exception
from ksadk.cli.ui import (
    get_console,
    new_table,
    print_error,
    print_info,
    print_kv,
    print_rule,
    print_success,
    print_title,
    print_warn,
    status_rich_style,
)

console = get_console()


@click.group("mcp")
def mcp():
    """MCP Server 管理命令
    
    \b
    示例:
        # 1) 默认部署
        agentengine mcp deploy .
        # 2) 常用查询
        agentengine mcp list
        # 3) 显式指定区域
        KSYUN_REGION=cn-beijing-6 agentengine mcp status <id>
    """
    pass


@mcp.command("deploy")
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
def deploy(mcp_dir: str, name: str, region: str, ks3_bucket: str, enable_auth: bool, dry_run: bool, artifact_type: str, no_cache: bool):
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
    dry_run = effective_dry_run(dry_run)
    run_async_with_dry_run(
        _deploy_mcp_async(mcp_dir, name, region, ks3_bucket, enable_auth, dry_run, artifact_type, no_cache),
        dry_run=dry_run,
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
    from ksadk.deployment.state import load_state, save_state, clear_state
    print_title("MCP 部署", f"region: {region}")
    print_kv("项目目录", str(mcp_path))
    
    # 1. 检测 MCP 项目
    detector = MCPDetector(str(mcp_path))
    detection_result = detector.detect()
    
    if not detection_result.is_valid:
        print_error("未检测到 FastMCP 项目")
        print_info("请确保项目包含 `from fastmcp import FastMCP`")
        return
    
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
        builder = MCPCodeBuilder(mcp_path)
        if no_cache:
            # 清除旧 zip 缓存，强制重新构建
            zip_path = builder.build_dir / f"{mcp_name}.zip"
            if zip_path.exists():
                zip_path.unlink()
                print_warn(f"已清除旧缓存: {zip_path.name}")
        build_result = builder.build()
        
        if not build_result.success:
            print_error(f"构建失败: {build_result.error_message}")
            return
        
        # 3. 上传到 KS3
        print_rule("上传到 KS3")
        from ksadk.builders.ks3_uploader import KS3Uploader
        # 让 KS3Uploader 使用默认 bucket 逻辑 (ACCOUNT_ID)
        uploader = KS3Uploader(region=upload_region, bucket=ks3_bucket)
        object_key = f"mcps/{mcp_name}/code.zip"
        
        ks3_path = await uploader.upload(build_result.artifact_path, object_key)
        
        if not ks3_path:
            print_error("KS3 上传失败")
            return
        
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
                print_error("Docker 未正常运行")
                return
        except FileNotFoundError:
            print_error("未找到 docker 命令，请先安装 Docker")
            return
        
        from ksadk.builders.container_builder import ContainerBuilder
        container_builder = ContainerBuilder(
            project_dir=mcp_path,
            no_cache=no_cache,
        )
        # 利用 ContainerBuilder 内置的 build()，它会自动检测项目、确定镜像名和推送
        build_result = container_builder.build()
        
        if not build_result.success:
            print_error(f"Docker 镜像构建失败: {build_result.error_message}")
            return
        
        image_name = build_result.metadata.get("image")
        print_success(f"镜像构建成功: {image_name}")
        
        # 3. 推送镜像
        print_rule("推送镜像到仓库")
        if not container_builder.push(image_name):
            print_error("镜像推送失败")
            return
        
        print_success(f"推送成功: {image_name}")
        artifact_path_final = image_name
    else:
        print_error(f"不支持的 artifact_type: {artifact_type}")
        return

    if dry_run:
        print_rule("[Dry Run] 请求数据")
        import json
        request_data_preview = {
            "name": mcp_name,
            "type": "mcp",
            "artifact_type": artifact_type,
            "artifact_path": artifact_path_final,
            "region": region,
            "tools": detection_result.tools,
        }
        console.print(json.dumps(request_data_preview, indent=2, ensure_ascii=False))
        return
    
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
    
    try:
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
                    raise Exception("Server 返回空响应，可能是 MCP 名称已存在或 Server 内部错误，请查看 Server 日志")
                mcp_id = res.get("mcp_id")
            
            endpoint = res.get("endpoint")
            
            # 保存本地状态
            from datetime import datetime
            
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
            
    except Exception as e:
        print_exception("部署失败", e)


@mcp.command("list")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@dry_run_option()
def list_mcps(region: str, dry_run: bool):
    """列出已部署的 MCP"""
    dry_run = effective_dry_run(dry_run)
    from ksadk.api import AgentEngineClient
    
    async def _list():
            
        try:
            async with AgentEngineClient(region=region, dry_run=dry_run) as client:
                resp = await client.list_mcps(region=region)
                
                mcps = resp.get("mcps", [])
                total = resp.get("total", 0)
                
                if not mcps:
                    print_warn("没有找到已部署的 MCP Server")
                    return

                table = new_table(f"已部署 MCP Server [muted](总计: {total})[/]")
                table.add_column("ID", style="#58a6ff", no_wrap=True)
                table.add_column("NAME", style="white")
                table.add_column("STATUS", no_wrap=True, justify="center")
                table.add_column("ENDPOINT", style="#8b949e", overflow="ellipsis")
                for m in mcps:
                    status = (m.get("status") or "UNKNOWN").upper()
                    table.add_row(
                        m.get("mcp_id", "-"),
                        m.get("name", "-"),
                        f"[{status_rich_style(status)}]{status}[/]",
                        m.get("mcp_endpoint", "N/A"),
                    )
                console.print(table)
                
        except DryRunExit:
            raise
        except Exception as e:
            print_exception("获取列表失败", e)

    run_async_with_dry_run(_list(), dry_run=dry_run)


@mcp.command("status")
@click.argument("mcp_id")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@dry_run_option()
def status(mcp_id: str, region: str, dry_run: bool):
    """查看 MCP 状态
    
    MCP_ID: MCP 的 ID
    """
    dry_run = effective_dry_run(dry_run)
    from ksadk.api import AgentEngineClient
    import json
    
    async def _get():
        server_url = os.getenv("AGENTENGINE_SERVER_URL")
        if not server_url:
            print_error("未配置 AGENTENGINE_SERVER_URL")
            return
            
        try:
            async with AgentEngineClient(region=region, dry_run=dry_run) as client:
                mcp = await client.get_mcp(mcp_id)
                
                print_title("MCP 状态", mcp.get("name", mcp_id))
                print_kv("ID", mcp.get("mcp_id", "-"))
                status = (mcp.get("status") or "UNKNOWN").upper()
                print_kv("Status", status, value_style=status_rich_style(status))
                print_kv("Region", mcp.get("region", "-"))
                print_kv("Endpoint", mcp.get("endpoint", "N/A"))
                print_kv("MCP URL", mcp.get("mcp_endpoint", "N/A"))
                print_kv("Auth", "Enabled" if mcp.get("enable_auth") else "Disabled")
                if mcp.get('tools'):
                    print_kv("Tools", ", ".join(mcp["tools"]))
                print_kv("Created", str(mcp.get("created_at")))
                print_kv("Updated", str(mcp.get("updated_at")))
                
        except DryRunExit:
            raise
        except Exception as e:
            print_exception("获取状态失败", e)

    run_async_with_dry_run(_get(), dry_run=dry_run)


@mcp.command("delete")
@click.argument("mcp_id")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--yes", "-y", is_flag=True, help="跳过确认")
@dry_run_option()
def delete(mcp_id: str, region: str, yes: bool, dry_run: bool):
    """删除 MCP
    
    MCP_ID: 要删除的 MCP ID
    """
    dry_run = effective_dry_run(dry_run)
    from ksadk.api import AgentEngineClient

    if not yes and not dry_run:
        if not click.confirm("确定要删除这个 MCP 吗?"):
            print_info("已取消")
            return
    
    async def _delete():
        server_url = os.getenv("AGENTENGINE_SERVER_URL")
        if not server_url:
            print_error("未配置 AGENTENGINE_SERVER_URL")
            return
            
        try:
            async with AgentEngineClient(region=region, dry_run=dry_run) as client:
                success = await client.delete_mcp(mcp_id)
                if success:
                    print_success(f"MCP 已删除: {mcp_id}")
                    
                    # 尝试清理本地状态文件 (如果在项目目录中)
                    from ksadk.deployment.state import clear_state
                    try:
                        # 假设在当前目录运行
                        removed = clear_state(Path("."), key=mcp_id)
                        if removed:
                            print_info("本地状态文件已清理")
                        else:
                            print_warn("未清理本地状态文件: 当前目录状态与目标 ID 不匹配")
                    except Exception:
                        pass
                else:
                    print_error("删除失败")
                
        except DryRunExit:
            raise
        except Exception as e:
            print_exception("删除失败", e)

    run_async_with_dry_run(_delete(), dry_run=dry_run)
