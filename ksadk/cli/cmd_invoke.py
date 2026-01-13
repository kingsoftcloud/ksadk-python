"""
agentengin invoke - 与已部署的 Agent 进行交互

支持 OpenAI 兼容格式调用，支持流式输出
"""

import click
import asyncio
import json
import sys
from pathlib import Path
from typing import Optional


@click.command()
@click.option('--agent', '-a', help='Agent 名称或 ID')
@click.option('--endpoint', '-e', help='Agent Endpoint URL (覆盖自动获取)')
@click.option('--message', '-m', help='发送的消息 (非交互模式)')
@click.option('--session', '-s', help='Session ID (可选)')
@click.option('--no-stream', is_flag=True, help='禁用流式输出')
@click.option('--region', '-r', default='cn-beijing-6', help='区域')
@click.option('--local', '-l', is_flag=True, help='连接本地服务 (http://localhost:8080)')
@click.option('--insecure', '-k', is_flag=True, help='跳过 SSL 证书验证 (类似 curl -k)')
def invoke(agent: str, endpoint: str, message: str, session: str, 
           no_stream: bool, region: str, local: bool, insecure: bool):
    """与 Agent 进行交互 (本地或远程)
    
    \b
    使用方式:
        agentengin invoke --local                  # 连接本地
        agentengin invoke --agent my-agent         # 连接云端
        agentengin invoke --endpoint http://...    # 指定地址
        agentengin invoke -k --endpoint https://... # 跳过 SSL 验证
    """
    # 确定 Endpoint
    if local:
        endpoint = "http://localhost:8080"
    elif not endpoint:
        if not agent:
            # 尝试从配置文件读取
            agent = _get_agent_from_config()
        
        if not agent:
            click.secho("❌ 请指定 --local, --agent 或 --endpoint 参数", fg='red')
            raise SystemExit(1)
        
        # 从 agent name 构造 endpoint (或调用 GetAgentRuntime API)
        endpoint = _get_endpoint(agent, region)
    
    click.secho(f"🤖 连接到 Agent", fg='blue', bold=True)
    click.echo(f"   Endpoint: {endpoint}")
    if insecure:
        click.secho("   ⚠️  SSL 证书验证已禁用", fg='yellow')
    
    stream = not no_stream
    
    if message:
        # 单次调用模式
        asyncio.run(_invoke_once(endpoint, message, session, stream, insecure))
    else:
        # 交互模式
        asyncio.run(_invoke_interactive(endpoint, session, stream, insecure))


def _get_agent_from_config() -> Optional[str]:
    """从配置文件读取 agent 名称"""
    import yaml
    
    config_path = Path('.') / 'agentengin.yaml'
    if not config_path.exists():
        config_path = Path('.') / 'ksadk.yaml'
    
    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f)
            return config.get('name')
    return None


def _get_endpoint(agent: str, region: str) -> str:
    """获取 Agent Endpoint
    
    TODO: 调用 GetAgentRuntime API 获取真实 endpoint
    目前使用约定格式
    """
    # 约定格式: https://{agent-id}.agent.kspmas.ksyun.com
    return f"https://{agent}.agent.kspmas.ksyun.com"


async def _invoke_once(endpoint: str, message: str, session_id: str, stream: bool, insecure: bool = False):
    """单次调用"""
    click.echo(f"\n👤 你: {message}")
    click.echo(f"🤖 Agent: ", nl=False)
    
    try:
        if stream:
            async for chunk in _stream_chat(endpoint, message, session_id, insecure):
                content = _extract_content(chunk)
                if content:
                    click.echo(content, nl=False)
            click.echo()  # 换行
        else:
            response = await _chat(endpoint, message, session_id, insecure)
            content = _extract_response_content(response)
            click.echo(content)
    except Exception as e:
        click.secho(f"\n❌ 调用失败: {e}", fg='red')


async def _invoke_interactive(endpoint: str, session_id: str, stream: bool, insecure: bool = False):
    """交互模式"""
    click.echo("\n输入 'exit' 或 'quit' 退出\n")
    
    # 如果没有指定 session_id，生成一个
    if not session_id:
        import uuid
        session_id = str(uuid.uuid4())[:8]
    
    while True:
        try:
            # 使用 sys.stdin 读取并处理可能的编码问题
            try:
                user_input = input("👤 你: ").strip()
            except UnicodeDecodeError:
                # 某些终端在中文输入时可能出现编码问题
                # 尝试使用 sys.stdin.buffer 读取原始字节并解码
                click.secho("\n⚠️  输入编码异常，请重试", fg='yellow')
                continue
            
            if not user_input:
                continue
            
            if user_input.lower() in ('exit', 'quit', '退出'):
                click.echo("\n👋 再见!")
                break
            
            click.echo(f"🤖 Agent: ", nl=False)
            
            if stream:
                async for chunk in _stream_chat(endpoint, user_input, session_id, insecure):
                    content = _extract_content(chunk)
                    if content:
                        click.echo(content, nl=False)
                click.echo()  # 换行
            else:
                response = await _chat(endpoint, user_input, session_id, insecure)
                content = _extract_response_content(response)
                click.echo(content)
            
            print()  # 空行
            
        except KeyboardInterrupt:
            click.echo("\n\n👋 再见!")
            break
        except EOFError:
            click.echo("\n👋 再见!")
            break
        except UnicodeDecodeError as e:
            # 捕获所有其他 Unicode 解码错误
            click.secho(f"\n⚠️  输入编码错误，请重新输入 (错误: {e})", fg='yellow')
            print()
        except Exception as e:
            # 检查是否是编码相关错误
            if 'codec' in str(e).lower() or 'decode' in str(e).lower():
                click.secho(f"\n⚠️  输入编码问题，请重新输入", fg='yellow')
            else:
                click.secho(f"\n❌ 错误: {e}", fg='red')
            print()



async def _chat(endpoint: str, message: str, session_id: str = None, insecure: bool = False) -> dict:
    """非流式调用 (OpenAI 兼容格式)"""
    try:
        import httpx
    except ImportError:
        click.secho("❌ 请安装 httpx: pip install httpx", fg='red')
        raise SystemExit(1)
    
    url = f"{endpoint.rstrip('/')}/v1/chat/completions"
    
    payload = {
        "messages": [{"role": "user", "content": message}],
        "stream": False
    }
    
    if session_id:
        payload["session_id"] = session_id
    
    # 本地请求禁用系统代理 (ClashX 等会导致本地请求 502 错误)
    # trust_env=False 会禁用: 代理设置、SSL 证书环境变量、.netrc 文件
    # 对本地请求通常无影响，因为不需要这些配置
    is_local = 'localhost' in url or '127.0.0.1' in url or '0.0.0.0' in url
    
    # 构造 httpx client 配置
    client_kwargs = {
        'timeout': 60,
        'trust_env': not is_local
    }
    
    # 如果指定了 --insecure 参数，跳过 SSL 证书验证（类似 curl -k）
    if insecure:
        client_kwargs['verify'] = False
    
    async with httpx.AsyncClient(**client_kwargs) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        return response.json()


async def _stream_chat(endpoint: str, message: str, session_id: str = None, insecure: bool = False):
    """流式调用 (SSE)"""
    try:
        import httpx
    except ImportError:
        click.secho("❌ 请安装 httpx: pip install httpx", fg='red')
        raise SystemExit(1)
    
    url = f"{endpoint.rstrip('/')}/v1/chat/completions"
    
    payload = {
        "messages": [{"role": "user", "content": message}],
        "stream": True
    }
    
    if session_id:
        payload["session_id"] = session_id
    
    # 本地请求禁用系统代理 (ClashX 等会导致本地请求 502 错误)
    # trust_env=False 会禁用: 代理设置、SSL 证书环境变量、.netrc 文件
    # 对本地请求通常无影响，因为不需要这些配置
    is_local = 'localhost' in url or '127.0.0.1' in url or '0.0.0.0' in url
    
    # 构造 httpx client 配置
    client_kwargs = {
        'timeout': 60,
        'trust_env': not is_local
    }
    
    # 如果指定了 --insecure 参数，跳过 SSL 证书验证（类似 curl -k）
    if insecure:
        client_kwargs['verify'] = False
    
    async with httpx.AsyncClient(**client_kwargs) as client:
        async with client.stream("POST", url, json=payload) as response:
            response.raise_for_status()
            
            # 使用自定义的缓冲区处理，防止 UTF-8 字符截断导致的解码错误
            buffer = b""
            async for chunk in response.aiter_bytes():
                buffer += chunk
                while b"\n" in buffer:
                    line_data, buffer = buffer.split(b"\n", 1)
                    line_data = line_data.strip()
                    if not line_data:
                        continue
                        
                    try:
                        line = line_data.decode("utf-8")
                    except UnicodeDecodeError:
                        # 如果这一行本身解码失败(极少见)，忽略或用 replace
                        line = line_data.decode("utf-8", errors="replace")
                    
                    if line.startswith("data: "):
                        data = line[6:]
                        
                        if data == "[DONE]":
                            break
                        
                        try:
                            yield json.loads(data)
                        except json.JSONDecodeError:
                            continue


def _extract_content(chunk: dict) -> str:
    """从 OpenAI 流式响应中提取内容"""
    # OpenAI 格式: {"choices": [{"delta": {"content": "xxx"}}]}
    try:
        choices = chunk.get("choices", [])
        if choices:
            delta = choices[0].get("delta", {})
            return delta.get("content", "")
    except (KeyError, IndexError):
        pass
    return ""


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
