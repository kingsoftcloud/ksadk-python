"""
agentengine mcp - MCP Server 管理 (预览)

支持操作:
- deploy: 部署 MCP Server 到云端
- list: 列出已部署的 MCP
- status: 查看 MCP 状态
- delete: 删除 MCP
"""

import os
import click
import asyncio
from pathlib import Path


@click.group("mcp")
def mcp():
    """MCP Server 管理命令
    
    \b
    示例:
        agentengine mcp deploy .       # 部署 MCP
        agentengine mcp list           # 列出已部署的 MCP
        agentengine mcp status <id>    # 查看状态
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
@click.option(
    "--dry-run",
    is_flag=True,
    help="仅显示请求内容，不实际部署"
)
def deploy(mcp_dir: str, name: str, region: str, ks3_bucket: str, enable_auth: bool, dry_run: bool):
    """部署 MCP Server 到云端
    
    \b
    MCP_DIR: MCP 项目目录 (默认: 当前目录)
    
    \b
    示例:
        agentengine mcp deploy .
        agentengine mcp deploy ./my-mcp --name my-tools
    
    \b
    部署后的 endpoint 兼容标准 MCP 协议，可以被:
        - LangGraph/LangChain (via langchain-mcp-adapters)
        - Google ADK (via MCPToolset)
        - Cursor / Claude Code
        - Dify 等外部平台
    """
    asyncio.run(_deploy_mcp_async(mcp_dir, name, region, ks3_bucket, enable_auth, dry_run))


async def _deploy_mcp_async(
    mcp_dir: str,
    name: str,
    region: str,
    ks3_bucket: str,
    enable_auth: bool,
    dry_run: bool
):
    """异步 MCP 部署流程"""
    from ksadk.detection.mcp_detector import MCPDetector
    from ksadk.builders.mcp_builder import MCPCodeBuilder
    from ksadk.builders.ks3_uploader import KS3Uploader
    from ksadk.api import AgentEngineClient
    
    mcp_path = Path(mcp_dir).resolve()
    from ksadk.deployment.state import load_state, save_state, clear_state
    
    click.echo(f"\n🔧 MCP 项目: {mcp_path}")
    
    # 1. 检测 MCP 项目
    detector = MCPDetector(str(mcp_path))
    detection_result = detector.detect()
    
    if not detection_result.is_valid:
        click.secho("❌ 未检测到 FastMCP 项目", fg='red')
        click.echo("   请确保项目包含 `from fastmcp import FastMCP`")
        return
    
    click.echo(f"   ✓ 检测到 FastMCP 项目")
    click.echo(f"   入口: {detection_result.entry_point}")
    click.echo(f"   MCP 变量: {detection_result.mcp_variable}")
    if detection_result.tools:
        click.echo(f"   工具: {', '.join(detection_result.tools)}")
    
    mcp_name = name or mcp_path.name.replace('-', '_').replace('.', '_')
    
    # 2. 构建
    click.echo(f"\n📦 构建 MCP: {mcp_name}")
    builder = MCPCodeBuilder(mcp_path)
    build_result = builder.build()
    
    if not build_result.success:
        click.secho(f"❌ 构建失败: {build_result.error_message}", fg='red')
        return
    
    # 3. 上传到 KS3
    click.echo("\n☁️  上传到 KS3...")
    upload_region = "cn-beijing-6" if region == "pre-online" else region
    
    # 让 KS3Uploader 使用默认 bucket 逻辑 (ACCOUNT_ID)
    uploader = KS3Uploader(region=upload_region, bucket=ks3_bucket)
    object_key = f"mcps/{mcp_name}/code.zip"
    
    ks3_path = await uploader.upload(build_result.artifact_path, object_key)
    
    if not ks3_path:
        click.secho("❌ KS3 上传失败", fg='red')
        return
    
    click.echo(f"   ✓ 上传成功: {ks3_path}")
    
    if dry_run:
        click.echo("\n📋 [Dry Run] 请求数据:")
        import json
        request_data = {
            "name": mcp_name,
            "type": "mcp",
            "artifact_path": ks3_path,
            "region": region,
            "tools": detection_result.tools,
        }
        click.echo(json.dumps(request_data, indent=2, ensure_ascii=False))
        return
    
    # 4. 读取本地状态文件
    state = load_state(mcp_path)
    existing_mcp_id = None
    
    if state.get("type") == "mcp":
        existing_mcp_id = state.get("mcp_id")
    
    # 5. 调用 Server API (使用 AgentEngineClient 默认内网地址)
    click.echo("\n🚀 部署 MCP Server...")
    
    from ksadk.common.auth import AWSV4Auth
    auth = AWSV4Auth()
    
    request_data = {
        "name": mcp_name,
        "type": "mcp",
        "artifact_type": "Code",
        "artifact_path": ks3_path,
        "region": region,
        "enable_auth": enable_auth,  # 可选 API Key 保护
        "resources": {"cpu": "1", "memory": "2Gi"},
        "scaling": {"min_replicas": 1, "max_replicas": 5, "concurrency": 20},
        "metadata": {
            "mcp_variable": detection_result.mcp_variable,
            "tools": detection_result.tools,
        }
    }
    
    if auth.access_key_id and auth.secret_access_key:
        request_data["ks3"] = {
            "access_key": auth.access_key_id,
            "secret_key": auth.secret_access_key,
            "region": upload_region,
            "bucket": uploader.bucket_name,
        }
    
    try:
        async with AgentEngineClient(region=region) as client:
            if existing_mcp_id:
                # 更新
                click.echo(f"   检测到本地状态: {existing_mcp_id}")
                click.echo(f"   执行热更新...")
                res = await client.update_mcp(existing_mcp_id, request_data)
                mcp_id = existing_mcp_id
            else:
                # 创建
                res = await client.create_mcp(request_data)
                mcp_id = res.get("mcp_id")
            
            endpoint = res.get("endpoint")
            
            # 保存本地状态
            from datetime import datetime
            
            state_data = {
                "type": "mcp",
                "mcp_id": mcp_id,
                "name": mcp_name,
                "region": region,
                "endpoint": endpoint,
                "mcp_endpoint": f"{endpoint}/mcp" if endpoint else None,
                "tools": detection_result.tools,
            }
            if res.get("api_key"):
                state_data["api_key"] = res.get("api_key")
            
            save_state(mcp_path, state_data)
            
            click.echo(f"   💾 已保存状态到 .agentengine.state")
            
            # 输出结果
            click.secho(f"\n✅ MCP 部署成功!", fg='green')
            click.echo(f"   MCP ID: {mcp_id}")
            if endpoint:
                click.echo(f"   Endpoint: {endpoint}")
                click.echo(f"   MCP URL: {endpoint}/mcp")
            click.echo(f"\n📖 调用方式:")
            click.echo(f"   # Cursor/Claude 配置:")
            click.echo(f'   {{"url": "{endpoint}/mcp"}}')
            click.echo(f"\n   # LangChain/LangGraph:")
            click.echo(f'   from langchain_mcp_adapters.client import MCPClientToolkit')
            click.echo(f'   toolkit = MCPClientToolkit(url="{endpoint}/mcp")')
            click.echo(f"\n   # ADK:")
            click.echo(f'   from google.adk.tools.mcp_tool import MCPToolset')
            click.echo(f'   mcp = MCPToolset.from_server(connection_params={{')
            click.echo(f'       "url": "{endpoint}/mcp"')
            click.echo(f'   }})')
            
    except Exception as e:
        click.secho(f"❌ 部署失败: {e}", fg='red')


@mcp.command("list")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
def list_mcps(region: str):
    """列出已部署的 MCP"""
    from ksadk.api import AgentEngineClient
    import asyncio
    
    async def _list():
        server_url = os.getenv("AGENTENGINE_SERVER_URL")
        if not server_url:
            click.secho("❌ 未配置 AGENTENGINE_SERVER_URL", fg='red')
            return
            
        try:
            async with AgentEngineClient(region=region) as client:
                resp = await client.list_mcps()
                
                mcps = resp.get("mcps", [])
                total = resp.get("total", 0)
                
                if not mcps:
                    click.echo("没有找到已部署的 MCP Server")
                    return
                
                click.echo(f"\n📋 已部署的 MCP Server ({total}):\n")
                
                # 表头
                click.echo(f"  {'ID':<24} {'NAME':<20} {'STATUS':<10} {'ENDPOINT'}")
                click.echo(f"  {'-'*24} {'-'*20} {'-'*10} {'-'*40}")
                
                for m in mcps:
                    status_color = 'green' if m['status'] == 'Running' else 'yellow' if m['status'] == 'Creating' else 'red'
                    click.echo(f"  {m['mcp_id']:<24} {m['name']:<20} {click.style(m['status'], fg=status_color):<10} {m.get('mcp_endpoint', 'N/A')}")
                click.echo("")
                
        except Exception as e:
            click.secho(f"❌ 获取列表失败: {e}", fg='red')

    asyncio.run(_list())


@mcp.command("status")
@click.argument("mcp_id")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
def status(mcp_id: str, region: str):
    """查看 MCP 状态
    
    MCP_ID: MCP 的 ID
    """
    from ksadk.api import AgentEngineClient
    import asyncio
    import json
    
    async def _get():
        server_url = os.getenv("AGENTENGINE_SERVER_URL")
        if not server_url:
            click.secho("❌ 未配置 AGENTENGINE_SERVER_URL", fg='red')
            return
            
        try:
            async with AgentEngineClient(region=region) as client:
                mcp = await client.get_mcp(mcp_id)
                
                click.echo(f"\n📊 MCP 状态: {click.style(mcp['name'], bold=True)}")
                click.echo(f"   ID: {mcp['mcp_id']}")
                click.echo(f"   Status: {click.style(mcp['status'], fg='green' if mcp['status']=='Running' else 'yellow')}")
                click.echo(f"   Region: {mcp['region']}")
                click.echo(f"   Endpoint: {mcp.get('endpoint', 'N/A')}")
                click.echo(f"   MCP URL: {mcp.get('mcp_endpoint', 'N/A')}")
                click.echo(f"   Auth: {'Enabled' if mcp.get('enable_auth') else 'Disabled'}")
                if mcp.get('tools'):
                    click.echo(f"   Tools: {', '.join(mcp['tools'])}")
                
                click.echo(f"\n   Created: {mcp.get('created_at')}")
                click.echo(f"   Updated: {mcp.get('updated_at')}")
                
        except Exception as e:
            click.secho(f"❌ 获取状态失败: {e}", fg='red')

    asyncio.run(_get())


@mcp.command("delete")
@click.argument("mcp_id")
@click.confirmation_option(prompt="确定要删除这个 MCP 吗?")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
def delete(mcp_id: str, region: str):
    """删除 MCP
    
    MCP_ID: 要删除的 MCP ID
    """
    from ksadk.api import AgentEngineClient
    import asyncio
    
    async def _delete():
        server_url = os.getenv("AGENTENGINE_SERVER_URL")
        if not server_url:
            click.secho("❌ 未配置 AGENTENGINE_SERVER_URL", fg='red')
            return
            
        try:
            async with AgentEngineClient(region=region) as client:
                success = await client.delete_mcp(mcp_id)
                if success:
                    click.secho(f"\n✅ MCP 已删除: {mcp_id}", fg='green')
                    
                    # 尝试清理本地状态文件 (如果在项目目录中)
                    from ksadk.deployment.state import clear_state
                    try:
                        # 假设在当前目录运行
                        clear_state(Path("."), key=mcp_id)
                        click.echo(f"   🗑️  本地状态文件已清理")
                    except Exception:
                        pass
                else:
                    click.secho(f"\n❌ 删除失败", fg='red')
                
        except Exception as e:
            click.secho(f"❌ 删除失败: {e}", fg='red')

    asyncio.run(_delete())
