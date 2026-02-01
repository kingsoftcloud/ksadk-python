"""
agentengine invoke - 与已部署的 Agent 进行交互

支持 OpenAI 兼容格式调用，支持流式输出
"""

import click
import asyncio
import json
from pathlib import Path
from typing import Optional
import time

try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.live import Live
    console = Console()
except ImportError:
    console = None
    Markdown = None
    Live = None


@click.command()
@click.option("--agent", "-a", help="Agent 名称或 ID")
@click.option("--endpoint", "-e", help="Agent Endpoint URL (覆盖自动获取)")
@click.option("--api-key", help="AgentEngine API Key (覆盖本地配置)")
@click.option("--message", "-m", help="发送的消息 (单次调用模式)")
@click.option("--session", "-s", help="Session ID (可选)")
@click.option("--region", "-r", default="cn-beijing-6", envvar="KSYUN_REGION", help="区域")
@click.option("--local", "-l", is_flag=True, help="连接本地服务 (http://localhost:8080)")
@click.option("--insecure", "-k", is_flag=True, help="跳过 SSL 证书验证 (类似 curl -k)")
@click.option("--model", help="指定模型名称")
@click.option("--show-thinking", is_flag=True, help="显示模型思考过程")
def invoke(
    agent: str,
    endpoint: str,
    api_key: str,
    message: str,
    session: str,
    region: str,
    local: bool,
    insecure: bool,
    model: str,
    show_thinking: bool,
):
    """与 Agent 进行交互 (本地或远程)

    默认使用 TUI 交互模式，使用 -m 发送单条消息。

    \\b
    使用方式:
        agentengine invoke --local                  # 本地 TUI 模式
        agentengine invoke --agent my-agent         # 云端 TUI 模式
        agentengine invoke --local -m "你好"        # 单次调用
        agentengine invoke -k --endpoint https://... # 跳过 SSL 验证
    """
    # 加载本地状态
    state = _load_state()
    
    # 确定 Endpoint
    if local:
        endpoint = "http://localhost:8080"
    elif not endpoint:
        # 如果没指明 endpoint，优先从命令行参数 agent 找，然后从 state 找，最后从 config 找
        target_agent = agent or state.get("agent_id") or state.get("name") or _get_agent_from_config()
        
        if not target_agent:
            click.secho("❌ 请指定 --local, --agent 或 --endpoint 参数", fg="red")
            raise SystemExit(1)

        # 优先使用 state 里的 endpoint (如果是对应的 agent)
        if not agent or agent == state.get("agent_id") or agent == state.get("name"):
            endpoint = state.get("endpoint")
            
        if not endpoint:
            # 自动获取
            endpoint = _get_endpoint(target_agent, region)

    # API Key
    api_key = api_key or state.get("api_key")

    click.secho(f"🤖 连接到 Agent", fg="blue", bold=True)
    click.echo(f"   Endpoint: {endpoint}")
    if api_key:
        click.echo(f"   Auth:     Bearer {api_key[:4]}****")
    else:
        click.secho("   ⚠️  未发现 API Key，尝试匿名调用", fg="yellow")
    
    if insecure:
        click.secho("   ⚠️  SSL 证书验证已禁用", fg="yellow")

    if message:
        # 单次调用模式
        asyncio.run(_invoke_once(endpoint, message, api_key, session, True, insecure, model))
    else:
        # 默认 TUI 模式
        _invoke_tui(endpoint, api_key, session, insecure, model, show_thinking)




def _invoke_tui(
    endpoint: str,
    api_key: str = None,
    session_id: str = None,
    insecure: bool = False,
    model: str = None,
    show_thinking: bool = False,
):
    """使用 TUI 模式调用"""
    from ksadk.runners.remote_runner import RemoteRunner
    from ksadk.tui import AgentTUI

    runner = RemoteRunner(
        endpoint=endpoint,
        api_key=api_key,
        session_id=session_id,
        insecure=insecure,
        model=model,
    )

    app = AgentTUI(
        runner=runner,
        show_thinking=show_thinking,
        project_dir=".",
    )
    app.run()


def _get_agent_from_config() -> Optional[str]:
    """从配置文件读取 agent 名称"""
    import yaml

    config_path = Path(".") / "agentengine.yaml"
    if not config_path.exists():
        config_path = Path(".") / "ksadk.yaml"

    if config_path.exists():
        # 使用 utf-8-sig 自动处理 BOM，确保 Windows 兼容性
        with open(config_path, encoding='utf-8-sig') as f:
            config = yaml.safe_load(f)
            return config.get("name")
    return None


def _load_state() -> dict:
    """从 .agentengine.state 加载状态"""
    import yaml
    state_file = Path(".") / ".agentengine.state"
    if state_file.exists():
        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return {}


def _get_api_key() -> Optional[str]:
    """兼容旧代码"""
    return _load_state().get("api_key")


def _get_endpoint(agent: str, region: str) -> str:
    """获取 Agent Endpoint"""
    from ksadk.api import AgentEngineClient
    import asyncio

    async def _get():
        async with AgentEngineClient(region=region) as client:
            res = await client.get_agent(agent)
            return res.get("endpoint", "")

    try:
        endpoint = asyncio.run(_get())
        if not endpoint:
             click.secho(f"⚠️  Agent '{agent}' 未返回 Endpoint，尝试使用默认格式", fg="yellow")
             return f"https://{agent}.agent.kspmas.ksyun.com"
        return endpoint
    except Exception as e:
        # 如果是本地开发环境或者连接失败，降级处理
        click.secho(f"⚠️  获取 Endpoint 失败: {e}，尝试使用默认格式", fg="yellow")
        return f"https://{agent}.agent.kspmas.ksyun.com"


async def _invoke_once(
    endpoint: str,
    message: str,
    api_key: str = None,
    session_id: str = None,
    stream: bool = True,
    insecure: bool = False,
    model: str = None,
):
    """单次调用"""
    click.echo(f"\n👤 你: {message}")
    click.echo(f"🤖 Agent: ", nl=False)

    try:
        if stream:
            full_response = ""
            if Live and Markdown:
                # 降低刷新率减少闪烁，vertical_overflow="visible"防止回滚丢失
                # 手动控制刷新以减少闪烁
                with Live(Markdown("", justify="left"), console=console, auto_refresh=False, vertical_overflow="visible") as live:
                    last_refresh_time = 0
                    full_reasoning = ""
                    async for chunk in _stream_chat(endpoint, message, api_key, session_id, True, insecure, model):
                        content, reasoning = _extract_content(chunk)
                        
                        updated = False
                        if reasoning:
                            full_reasoning += reasoning
                            updated = True
                        if content:
                            full_response += content
                            updated = True
                            
                        if updated:
                            # 构造显示文本
                            display_text = ""
                            if full_reasoning:
                                formatted_reasoning = full_reasoning.replace('\n', '\n> ')
                                display_text += f"> 🧠 **Thinking:**\n> {formatted_reasoning}\n\n"
                            display_text += full_response
                            
                            live.update(Markdown(display_text, justify="left"))
                            
                            # 基于时间限流刷新 (每0.2秒一次 = 5 FPS)
                            now = time.time()
                            if now - last_refresh_time > 0.2:
                                live.refresh()
                                last_refresh_time = now
                    live.refresh() # 确保最后一次刷新
            else:
               async for chunk in _stream_chat(endpoint, message, api_key, session_id, True, insecure, model):
                    content, reasoning = _extract_content(chunk)
                    if reasoning:
                        click.secho(reasoning, fg="bright_black", nl=False)
                    if content:
                        print(content, end="", flush=True)
            click.echo()  # 换行
        else:
            response = await _chat(endpoint, message, api_key, session_id, insecure, model)
            content = _extract_response_content(response)
            if console and Markdown:
                console.print(Markdown(content))
            else:
                click.echo(content)
    except Exception as e:
        click.secho(f"\n❌ 调用失败: {e}", fg="red")



async def _chat(
    endpoint: str,
    message: str,
    api_key: str = None,
    session_id: str = None,
    insecure: bool = False,
    model: str = None,
) -> dict:
    """非流式调用 (OpenAI 兼容格式)"""
    try:
        import httpx
    except ImportError:
        click.secho("❌ 请安装 httpx: pip install httpx", fg="red")
        raise SystemExit(1)

    url = f"{endpoint.rstrip('/')}/v1/chat/completions"

    payload = {"messages": [{"role": "user", "content": message}], "stream": False}

    if session_id:
        payload["session_id"] = session_id

    if model:
        payload["model"] = model

    # 本地请求禁用系统代理 (ClashX 等会导致本地请求 502 错误)
    # trust_env=False 会禁用: 代理设置、SSL 证书环境变量、.netrc 文件
    # 对本地请求通常无影响，因为不需要这些配置
    is_local = "localhost" in url or "127.0.0.1" in url or "0.0.0.0" in url

    # 构造 httpx client 配置
    client_kwargs = {"timeout": 60, "trust_env": not is_local}

    # 如果指定了 --insecure 参数，跳过 SSL 证书验证（类似 curl -k）
    if insecure:
        client_kwargs["verify"] = False

    # 构造 Headers
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()


async def _stream_chat(
    endpoint: str,
    message: str,
    api_key: str = None,
    session_id: str = None,
    is_once: bool = False,
    insecure: bool = False,
    model: str = None,
):
    """流式调用 (SSE)"""
    try:
        import httpx
    except ImportError:
        click.secho("❌ 请安装 httpx: pip install httpx", fg="red")
        raise SystemExit(1)

    url = f"{endpoint.rstrip('/')}/v1/chat/completions"

    payload = {"messages": [{"role": "user", "content": message}], "stream": True}

    if session_id:
        payload["session_id"] = session_id

    if model:
        payload["model"] = model

    # 本地请求禁用系统代理 (ClashX 等会导致本地请求 502 错误)
    # trust_env=False 会禁用: 代理设置、SSL 证书环境变量、.netrc 文件
    # 对本地请求通常无影响，因为不需要这些配置
    is_local = "localhost" in url or "127.0.0.1" in url or "0.0.0.0" in url

    # 构造 httpx client 配置
    client_kwargs = {"timeout": 60, "trust_env": not is_local}

    # 如果指定了 --insecure 参数，跳过 SSL 证书验证（类似 curl -k）
    if insecure:
        client_kwargs["verify"] = False

    # 构造 Headers
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(**client_kwargs) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as response:
            response.raise_for_status()
            try:
                # Use aiter_lines() for robust UTF-8 decoding and line splitting
                async for line in response.aiter_lines():
                    if not line:
                        continue

                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break

                        try:
                            data = json.loads(data_str)
                            # 直接 yield 解析后的 JSON 数据，让 _extract_content 处理
                            yield data

                            # Handle events/errors
                            if "error" in data:
                                click.secho(f"\nError: {data['error']}", fg="red")

                        except json.JSONDecodeError:
                            pass
            except Exception as e:
                click.secho(f"\nStream error: {e}", fg="red")


def _extract_content(chunk: dict) -> tuple[str, str]:
    """从 OpenAI 流式响应中提取内容 (包含 reasoning_content)"""
    # OpenAI 格式: {"choices": [{"delta": {"content": "xxx", "reasoning_content": "thought"}}]}
    try:
        choices = chunk.get("choices", [])
        if choices:
            delta = choices[0].get("delta", {})
            return delta.get("content", "") or "", delta.get("reasoning_content", "") or ""
    except (KeyError, IndexError):
        pass
    return "", ""


def _extract_response_content(response: dict) -> str:
    """从 OpenAI 非流式响应中提取内容"""
    # OpenAI 格式: {"choices": [{"message": {"content": "xxx"}}]}
    try:
        choices = response.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
            return message.get("content", "")
    except (KeyError, IndexError):
        pass
    return str(response)
