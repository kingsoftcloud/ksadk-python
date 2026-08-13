"""A2A Space 服务地址与凭证解析（对齐 ksadk/skills 的 KOP pull 模式）。

runtime 通过 KOP 公网网关访问 agentengine-server 的对外 A2A Action
（ListAToASpaceAgents，返回里已含完整投影后 AgentCard），与 Skill Space
走同一套 AICP 连接解析：默认 aicp.api.ksyun.com，可被 KSADK_A2A_SERVICE_URL 覆盖。
runtime 拿到 card 后直接用 card.url 调用，不需要 gateway 域名。
"""

from __future__ import annotations

import os

from ksadk.common.aicp_env import resolve_aicp_connection

ENV_A2A_SERVICE_URL = "KSADK_A2A_SERVICE_URL"
ENV_A2A_SERVICE_TOKEN = "KSADK_A2A_SERVICE_TOKEN"


def resolve_a2a_service_url() -> str:
    """返回 A2A 控制面对外服务 origin（不含 path）。

    优先 KSADK_A2A_SERVICE_URL；否则用 AICP 连接解析（aicp.api.ksyun.com）。
    """
    explicit = os.environ.get(ENV_A2A_SERVICE_URL, "").strip()
    if explicit:
        return explicit
    connection = resolve_aicp_connection("KSADK_A2A_SERVICE")
    return f"{connection['scheme']}://{connection['endpoint']}".rstrip("/")


def resolve_a2a_service_token() -> str:
    """返回 A2A 控制面服务 token（平台注入）。"""
    return os.environ.get(ENV_A2A_SERVICE_TOKEN, "").strip()
