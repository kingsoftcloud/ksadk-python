"""
agentengine openclaw - OpenClaw 云端部署管理

设计目标:
- 和 Agent 部署完全一致，复用 CreateProduct 接口 (Container 模式)
- Framework 标记为 "openclaw"，区分于普通 Agent
- 预构建公共镜像，用户无需自行构建
- 模型配置通过 EnvironmentVariables 传递，自动复用 OPENAI_* 变量
"""

from __future__ import annotations

import os
import asyncio
import secrets
from pathlib import Path
from typing import Optional

import click

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

# 默认 OpenClaw 镜像 (KCR 个人版)
DEFAULT_OPENCLAW_NAMESPACE = "agentengine"
DEFAULT_OPENCLAW_REPO = "openclaw"
DEFAULT_OPENCLAW_VERSION = "latest"
DEFAULT_OPENCLAW_NAME = "openclaw-gateway"
DEFAULT_GATEWAY_TOKEN = "openclaw-default-token"


def _resolve_env(*keys: str, default: Optional[str] = None) -> Optional[str]:
    """按优先级从多个环境变量中获取值"""
    for key in keys:
        val = os.getenv(key)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    return default


def _build_openclaw_env_vars(
    *,
    model_base_url: Optional[str] = None,
    model_api_key: Optional[str] = None,
    default_model: Optional[str] = None,
    gateway_token: Optional[str] = None,
    model_provider_id: Optional[str] = None,
) -> dict:
    """构建 OpenClaw 所需的环境变量，自动复用 OPENAI_* 环境变量"""
    env = {}

    # 模型配置 (CLI 参数 > OPENCLAW_* > OPENAI_* > 默认值)
    base_url = (
        model_base_url
        or _resolve_env("OPENCLAW_MODEL_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE")
    )
    api_key = (
        model_api_key
        or _resolve_env("OPENCLAW_MODEL_API_KEY", "OPENAI_API_KEY")
    )
    model = (
        default_model
        or _resolve_env("OPENCLAW_DEFAULT_MODEL", "OPENAI_MODEL_NAME", "MODEL_NAME")
    )
    provider_id = (
        model_provider_id
        or _resolve_env("OPENCLAW_MODEL_PROVIDER_ID")
        or "openai"
    )
    token = (
        gateway_token
        or _resolve_env("OPENCLAW_GATEWAY_TOKEN")
        or DEFAULT_GATEWAY_TOKEN
    )
    model_api = _resolve_env("OPENCLAW_MODEL_API") or "openai-completions"

    env["OPENCLAW_GATEWAY_BIND"] = "lan"
    env["OPENCLAW_GATEWAY_AUTH_MODE"] = "token"
    env["OPENCLAW_GATEWAY_TOKEN"] = token
    env["OPENCLAW_MODEL_PROVIDER_ID"] = provider_id
    env["OPENCLAW_MODEL_API"] = model_api

    if base_url:
        env["OPENCLAW_MODEL_BASE_URL"] = base_url
    if api_key:
        env["OPENCLAW_MODEL_API_KEY"] = api_key
    if model:
        env["OPENCLAW_DEFAULT_MODEL"] = model

    # 额外的可选配置
    catalog = _resolve_env("OPENCLAW_MODEL_CATALOG_JSON")
    if catalog:
        env["OPENCLAW_MODEL_CATALOG_JSON"] = catalog
    origins = _resolve_env("OPENCLAW_ALLOWED_ORIGINS")
    if origins:
        env["OPENCLAW_ALLOWED_ORIGINS"] = origins

    return env


def _parse_image(image: Optional[str]) -> tuple[str, str, str]:
    """解析镜像地址为 (namespace, repo, version)

    支持格式:
    - hub.kce.ksyun.com/ns/repo:tag → (ns, repo, tag)
    - ns/repo:tag → (ns, repo, tag)
    - 无输入 → 使用默认值
    """
    if not image:
        return DEFAULT_OPENCLAW_NAMESPACE, DEFAULT_OPENCLAW_REPO, DEFAULT_OPENCLAW_VERSION

    # 去掉 registry 域名前缀 (hub.kce.ksyun.com/)
    path = image
    if "/" in path:
        parts = path.split("/")
        if "." in parts[0]:
            # 有 registry 域名，去掉
            parts = parts[1:]
        if len(parts) >= 2:
            ns = parts[0]
            repo_version = "/".join(parts[1:])
        else:
            ns = "default"
            repo_version = parts[0]
    else:
        ns = "default"
        repo_version = path

    # 拆分 repo:version
    if ":" in repo_version:
        repo, version = repo_version.rsplit(":", 1)
    else:
        repo = repo_version
        version = "latest"

    return ns, repo, version


@click.group("openclaw")
def openclaw():
    """OpenClaw 云端部署管理

    \b
    示例:
        # 1) 部署 OpenClaw 到云端
        agentengine openclaw deploy
        # 2) 查看已部署的 OpenClaw
        agentengine openclaw list
        # 3) 查看状态
        agentengine openclaw status <id>
        # 4) 删除
        agentengine openclaw delete <id>
    """
    pass


@openclaw.command("deploy")
@click.option("--name", "-n", default=None, help="OpenClaw 名称 (默认: openclaw-gateway)")
@click.option(
    "--region", "-r",
    default="cn-beijing-6",
    envvar="KSYUN_REGION",
    help="部署区域 (默认: cn-beijing-6)",
)
@click.option("--image", default=None, help="OpenClaw 镜像地址 (默认: 内置公共镜像)")
@click.option("--model-base-url", default=None, help="模型 Base URL (默认复用 OPENAI_BASE_URL)")
@click.option("--model-api-key", default=None, help="模型 API Key (默认复用 OPENAI_API_KEY)")
@click.option("--default-model", default=None, help="默认模型名 (默认复用 OPENAI_MODEL_NAME)")
@click.option("--gateway-token", default=None, help="网关 Token (默认自动生成)")
@click.option("--dry-run", is_flag=True, help="仅显示请求，不实际部署")
def deploy(
    name: Optional[str],
    region: str,
    image: Optional[str],
    model_base_url: Optional[str],
    model_api_key: Optional[str],
    default_model: Optional[str],
    gateway_token: Optional[str],
    dry_run: bool,
):
    """部署 OpenClaw 到云端

    \b
    通过 CreateProduct (Container 模式) 部署预构建的 OpenClaw 镜像。
    模型配置自动复用 OPENAI_* 环境变量。

    \b
    示例:
        # 默认部署 (自动复用 .env 中的 OPENAI_* 变量)
        agentengine openclaw deploy
        # 显式指定模型
        agentengine openclaw deploy --model-base-url https://api.example.com/v1 --model-api-key sk-xxx
        # 使用自定义镜像
        agentengine openclaw deploy --image hub.kce.ksyun.com/myns/openclaw:v2
    """
    asyncio.run(
        _deploy_openclaw(
            name=name,
            region=region,
            image=image,
            model_base_url=model_base_url,
            model_api_key=model_api_key,
            default_model=default_model,
            gateway_token=gateway_token,
            dry_run=dry_run,
        )
    )


async def _deploy_openclaw(
    *,
    name: Optional[str],
    region: str,
    image: Optional[str],
    model_base_url: Optional[str],
    model_api_key: Optional[str],
    default_model: Optional[str],
    gateway_token: Optional[str],
    dry_run: bool,
):
    """异步部署 OpenClaw"""
    from ksadk.api import AgentEngineClient
    from ksadk.deployment.state import load_state, save_state

    if name:
        openclaw_name = name
    else:
        from datetime import datetime
        ts = datetime.now().strftime("%m%d%H%M")
        openclaw_name = f"{DEFAULT_OPENCLAW_NAME}-{ts}"
    ns, repo, version = _parse_image(image)

    # 构建环境变量
    env_vars = _build_openclaw_env_vars(
        model_base_url=model_base_url,
        model_api_key=model_api_key,
        default_model=default_model,
        gateway_token=gateway_token,
    )

    print_title("OpenClaw 云端部署", f"region: {region}")
    print_kv("名称", openclaw_name)
    print_kv("镜像", f"{ns}/{repo}:{version}")
    print_kv("区域", region, value_style="#58a6ff")

    if not env_vars.get("OPENCLAW_MODEL_API_KEY"):
        print_warn("未检测到模型 API Key (OPENAI_API_KEY / OPENCLAW_MODEL_API_KEY)")
        print_warn("OpenClaw 可启动，但无法正常调用模型")

    # 读取本地状态 (判断创建 vs 更新)
    project_dir = Path(".").resolve()
    state = load_state(project_dir)
    existing_agent_id = None
    if state.get("type") == "openclaw":
        existing_agent_id = state.get("agent_id")

    # 构建环境变量列表
    env_list = [
        {"Key": k, "Value": v, "IsSensitive": "KEY" in k or "TOKEN" in k or "SECRET" in k}
        for k, v in env_vars.items()
    ]

    # 构建请求数据
    request_data = {
        "name": openclaw_name,
        "description": "OpenClaw Gateway (managed by AgentEngine)",
        "framework": "openclaw",
        "artifact_type": "Container",
        "artifact_path": f"{ns}/{repo}:{version}",  # 会被 client 拆分
        "region": region,
        "resources": {"cpu": "2", "memory": "8Gi"},
        "scaling": {"min_replicas": 1, "max_replicas": 3, "concurrency": 20},
        "env_vars": env_list,
    }

    # KCR 凭证 (Container 模式需要)
    kcr_username = _resolve_env("KCR_USERNAME", "KSYUN_ACCOUNT_ID")
    kcr_password = _resolve_env("KCR_PASSWORD")
    if kcr_username:
        request_data["image_credential"] = {
            "username": kcr_username,
            "password": kcr_password or "",
        }
    if not kcr_password:
        print_warn("未配置 KCR_PASSWORD，私有镜像可能无法拉取 (公共镜像可忽略)")
        print_info("获取方式: https://kcr.console.ksyun.com/ → 访问凭证")

    if dry_run:
        import json
        print_rule("Dry Run — 请求数据")
        console.print(json.dumps(request_data, indent=2, ensure_ascii=False))
        return

    # 调用 API
    print_rule("部署 OpenClaw")
    try:
        async with AgentEngineClient(region=region) as client:
            if existing_agent_id:
                print_info(f"检测到本地状态: {existing_agent_id}，执行更新...")
                res = await client.update_agent(existing_agent_id, {
                    "artifact_path": f"{ns}/{repo}:{version}",
                    "env_vars": env_list,
                })
                agent_id = existing_agent_id
                endpoint = res.get("endpoint") or state.get("endpoint")
                api_key = state.get("api_key")
            else:
                res = await client.create_agent(request_data)
                if not res:
                    raise Exception("Server 返回空响应，请查看 Server 日志")

                # CreateProduct 返回 order_id，需要轮询获取 agent_id
                order_id = res.get("order_id")
                agent_id = res.get("agent_id")
                endpoint = res.get("endpoint")
                api_key = res.get("api_key")

                if order_id and not agent_id:
                    print_info(f"订单已创建: {order_id}，等待实例创建...")
                    import time
                    for i in range(12):  # 最多等 60s
                        time.sleep(5)
                        try:
                            detail = await client.get_agent(name=openclaw_name, include_api_key=True)
                            qa = detail.get("quick_access", {})
                            basic = detail.get("basic", {})
                            agent_id = basic.get("agent_id")
                            endpoint = qa.get("public_endpoint") or endpoint
                            api_key = qa.get("api_key") or api_key
                            if agent_id:
                                print_success(f"实例已创建: {agent_id}")
                                break
                        except Exception:
                            pass
                        print_info(f"等待中... ({(i+1)*5}s)")

                    if not agent_id:
                        print_warn("实例创建中，稍后使用 'agentengine openclaw list' 查看")

            # 保存状态
            save_state(project_dir, {
                "type": "openclaw",
                "agent_id": agent_id,
                "name": openclaw_name,
                "region": region,
                "endpoint": endpoint,
                "api_key": api_key,
                "image": f"{ns}/{repo}:{version}",
                "gateway_token": env_vars.get("OPENCLAW_GATEWAY_TOKEN"),
            })

            print_success("OpenClaw 部署成功")
            print_kv("Agent ID", agent_id or "(创建中)")
            if endpoint:
                print_kv("Endpoint", endpoint, value_style="#58a6ff")
            if api_key:
                print_kv("API Key", api_key, value_style="#d29922")
            token = env_vars.get("OPENCLAW_GATEWAY_TOKEN", "")
            if token and endpoint:
                print_kv("控制台", f"{endpoint}/#token={token}", value_style="#58a6ff")
            print_info("已保存状态到 .agentengine.state")

    except Exception as e:
        print_error(f"部署失败: {e}")


@openclaw.command("list")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
def list_openclaws(region: str):
    """列出已部署的 OpenClaw 实例"""
    from ksadk.api import AgentEngineClient

    async def _list():
        try:
            async with AgentEngineClient(region=region) as client:
                resp = await client.list_agents(region=region)
                agents = resp.get("agents", [])

                # 过滤 framework=openclaw
                openclaws = [a for a in agents if a.get("framework") == "openclaw"]

                if not openclaws:
                    print_warn("没有找到已部署的 OpenClaw 实例")
                    return

                table = new_table(f"已部署 OpenClaw [muted](总计: {len(openclaws)})[/]")
                table.add_column("ID", style="#58a6ff", no_wrap=True)
                table.add_column("NAME", style="white")
                table.add_column("STATUS", no_wrap=True, justify="center")
                table.add_column("ENDPOINT", style="#8b949e", overflow="ellipsis")
                table.add_column("REGION", style="#8b949e")
                for a in openclaws:
                    status = (a.get("status") or "UNKNOWN").upper()
                    table.add_row(
                        a.get("agent_id", "-"),
                        a.get("name", "-"),
                        f"[{status_rich_style(status)}]{status}[/]",
                        a.get("endpoint", "N/A"),
                        a.get("region", "-"),
                    )
                console.print(table)

        except Exception as e:
            print_error(f"获取列表失败: {e}")

    asyncio.run(_list())


@openclaw.command("status")
@click.argument("agent_ref", required=False, default=None)
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
def status(agent_ref: Optional[str], region: str):
    """查看 OpenClaw 状态

    \b
    AGENT_REF: Agent ID 或名称 (可选，默认从 .agentengine.state 读取)
    """
    # 无参数时从本地状态读取
    if not agent_ref:
        from ksadk.deployment.state import load_state
        state = load_state(Path(".").resolve())
        if state.get("type") == "openclaw" and state.get("agent_id"):
            agent_ref = state["agent_id"]
            print_info(f"从本地状态读取: {agent_ref}")
        elif state.get("type") == "openclaw" and state.get("name"):
            agent_ref = state["name"]
        else:
            print_error("请指定 Agent ID 或在部署目录下运行")
            return
    from ksadk.api import AgentEngineClient

    async def _get():
        try:
            async with AgentEngineClient(region=region) as client:
                # 尝试按 ID 查询，失败则按 Name
                if agent_ref.startswith("ar-"):
                    agent = await client.get_agent(agent_id=agent_ref)
                else:
                    agent = await client.get_agent(name=agent_ref)

                if not agent:
                    print_error(f"未找到 OpenClaw: {agent_ref}")
                    return

                print_title("OpenClaw 状态", agent.get("name", agent_ref))
                print_kv("ID", agent.get("agent_id", "-"))

                status_val = (agent.get("status") or "UNKNOWN").upper()
                print_kv("Status", status_val, value_style=status_rich_style(status_val))
                print_kv("Framework", agent.get("framework", "-"))
                print_kv("Region", agent.get("region", "-"))
                print_kv("Endpoint", agent.get("endpoint", "N/A"), value_style="#58a6ff")
                print_kv("镜像", agent.get("artifact_path", "-"))
                print_kv("Created", str(agent.get("created_at", "-")))
                print_kv("Updated", str(agent.get("updated_at", "-")))

        except Exception as e:
            print_error(f"获取状态失败: {e}")

    asyncio.run(_get())


@openclaw.command("delete")
@click.argument("agent_ref")
@click.confirmation_option(prompt="确定要删除这个 OpenClaw 实例吗?")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
def delete(agent_ref: str, region: str):
    """删除 OpenClaw 实例

    AGENT_REF: Agent ID
    """
    from ksadk.api import AgentEngineClient

    async def _delete():
        try:
            async with AgentEngineClient(region=region) as client:
                success = await client.delete_agent(agent_ref)
                if success:
                    print_success(f"OpenClaw 已删除: {agent_ref}")

                    # 清理本地状态
                    from ksadk.deployment.state import clear_state
                    try:
                        clear_state(Path("."), key=agent_ref)
                        print_info("本地状态文件已清理")
                    except Exception:
                        pass
                else:
                    print_error("删除失败")

        except Exception as e:
            print_error(f"删除失败: {e}")

    asyncio.run(_delete())


@openclaw.command("dashboard")
@click.argument("agent_ref", required=False, default=None)
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
def dashboard(agent_ref: Optional[str], region: str):
    """在浏览器中打开 OpenClaw Dashboard

    \b
    AGENT_REF: Agent ID 或名称 (可选，默认从 .agentengine.state 读取)

    \b
    示例:
        # 从当前目录的 .agentengine.state 自动读取
        agentengine openclaw dashboard
        # 指定 Agent ID
        agentengine openclaw dashboard ar-xxx
    """
    import webbrowser

    endpoint = None
    token = None

    # 1. 尝试从本地状态读取
    if not agent_ref:
        from ksadk.deployment.state import load_state
        state = load_state(Path(".").resolve())
        if state.get("type") == "openclaw":
            endpoint = state.get("endpoint")
            token = state.get("gateway_token")
            if endpoint:
                print_info(f"从本地状态文件读取: {state.get('agent_id', '-')}")

    # 2. 如果本地没有或指定了 agent_ref，通过 API 获取
    if not endpoint and agent_ref:
        from ksadk.api import AgentEngineClient

        async def _get_endpoint():
            async with AgentEngineClient(region=region) as client:
                if agent_ref.startswith("ar-"):
                    agent = await client.get_agent(agent_id=agent_ref)
                else:
                    agent = await client.get_agent(name=agent_ref)
                return agent.get("endpoint") if agent else None

        try:
            endpoint = asyncio.run(_get_endpoint())
        except Exception as e:
            print_error(f"获取 Endpoint 失败: {e}")
            return

    if not endpoint:
        print_error("未找到 OpenClaw Endpoint")
        print_info("请先部署 OpenClaw:")
        print_info("  agentengine openclaw deploy")
        print_info("或指定 Agent ID:")
        print_info("  agentengine openclaw dashboard <agent-id>")
        return

    # 构建 Dashboard URL
    dashboard_url = endpoint.rstrip("/")
    if token:
        dashboard_url = f"{dashboard_url}/#token={token}"

    print_success("打开 OpenClaw Dashboard")
    print_kv("URL", dashboard_url, value_style="#58a6ff")

    webbrowser.open(dashboard_url)
