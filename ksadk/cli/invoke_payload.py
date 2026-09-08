"""ksadk invoke 的 OpenAI 兼容请求载荷构造。

从 cmd_invoke.py 抽出，避免 cli 模块继续膨胀（架构守护限制 1000 行，
cmd_invoke 已处于 legacy 白名单，只许缩不许涨）。
"""

from __future__ import annotations

from typing import Any, Optional


def build_chat_request(
    endpoint: str,
    message: str,
    *,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    api_format: str = "chat_completions",
    default_model: Optional[str] = None,
    stream: bool = False,
) -> tuple[str, dict[str, Any]]:
    """构造 (url, payload)，按 api_format 区分 chat/completions 与 responses。

    OpenClaw gateway 2026.7.1+ 的 /v1/responses 特殊处理:
    - input 传纯字符串，不再接受 {role, content} 对象数组（服务端自行包装 user turn）
    - 拒绝顶层 session_id，会话标识放 metadata 传递
    - model 必填且只接受 "openclaw"/"openclaw/<agentId>"（业务模型由 gateway 配置决定），
      未显式传 --model 时用 default_model 补默认路由值，避免 400
    """
    normalized_api_format = str(api_format or "chat_completions").strip().lower()
    if normalized_api_format == "responses":
        url = f"{endpoint.rstrip('/')}/v1/responses"
        payload: dict[str, Any] = {"input": message, "stream": stream}
    else:
        url = f"{endpoint.rstrip('/')}/v1/chat/completions"
        payload = {"messages": [{"role": "user", "content": message}], "stream": stream}

    if session_id:
        if normalized_api_format == "responses":
            payload.setdefault("metadata", {})["session_id"] = session_id
        else:
            payload["session_id"] = session_id

    if model:
        payload["model"] = model
    elif default_model and normalized_api_format == "responses":
        payload["model"] = default_model

    return url, payload
