"""ksadk 模型出口协议转换层(P1 草案,待 codex review).

把 OpenAI Responses API <-> Chat Completions API 的转换内化为 ksadk 的模型出口中间层,
让只发 responses 的 runtime(codex)能透明使用所有 chat 协议模型,用户零感知。

设计见 docs/responses-chat-protocol-proxy-plan.md。

模块边界:
- transform: 纯转换器(无环境依赖),请求/响应/流式状态机。
- config:    ProxyConfig 显式注入,不在导入时读 env。
- server:    create_app(config) app factory + ProxyServer 生命周期管理。
- protocol_proxy: 可独立运行的入口(读 env → config → uvicorn)。
"""

from .bootstrap import setup_proxy_redirect_if_enabled, teardown_proxy_redirect
from .cache import CapabilityCache
from .config import ProxyConfig
from .detect import ModelCapabilities, probe_responses_capability
from .gate import ProxyGate
from .namespace import build_restore_map, flatten_namespace_tool_name, flatten_request_namespaces
from .server import ProxyServer  # noqa: F401  (re-export)
from .transform import (
    Streamer,
    chat_to_response,
    convert_tool_choice,
    convert_tools,
    convert_usage,
    input_to_messages,
    responses_to_chat,
)

__all__ = [
    "CapabilityCache",
    "ModelCapabilities",
    "ProxyConfig",
    "ProxyGate",
    "ProxyServer",
    "Streamer",
    "build_restore_map",
    "chat_to_response",
    "convert_tool_choice",
    "convert_tools",
    "convert_usage",
    "flatten_namespace_tool_name",
    "flatten_request_namespaces",
    "input_to_messages",
    "probe_responses_capability",
    "responses_to_chat",
    "setup_proxy_redirect_if_enabled",
    "teardown_proxy_redirect",
]
