"""A2A AgentCard 构造 (goal-05,wire 1.0)。

契约(`a2a-center-productization-2026-07.md` §2.1):
- AgentCard 使用 ``supportedInterfaces[]``,每项 ``url`` / ``protocolBinding`` /
  ``protocolVersion: "1.0"``;**不用顶层 ``url``,不提供 0.3 fallback,不挂
  ``/.well-known/agent.json`` 旧路径**。
- 我方托管 Agent 7 月优先暴露 ``JSONRPC``,同时挂 SDK 已提供的 ``HTTP+JSON`` 路由。
- a2a-sdk 1.1.0 的 wire 对象是 protobuf(``a2a_pb2``),非 pydantic。
"""

from __future__ import annotations

from collections.abc import Sequence

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
)

#: wire 协议版本(契约 §2.1 固定 1.0)。
A2A_PROTOCOL_VERSION = "1.0"
#: 托管 Agent 的 A2A 路由路径(契约 §3.3:/a2a/jsonrpc 与 /a2a/v1/*)。
JSONRPC_PATH = "/a2a/jsonrpc"
REST_PATH_PREFIX = "/a2a/v1"

_DEFAULT_IO_MODES = ["text/plain"]


def build_agent_card(
    *,
    name: str,
    base_url: str,
    description: str = "",
    version: str = "1.0.0",
    skills: Sequence[str] | None = None,
    streaming: bool = True,
    provider: dict[str, str] | None = None,
) -> AgentCard:
    """构造符合 wire 1.0 的 AgentCard(supportedInterfaces)。

    参数:
        name: Agent 名称。
        base_url: 该 Agent 对外宣告的基础地址(各 interface url 由此拼出)。
        description: 描述。
        version: 业务版本。
        skills: 技能 id 列表(空则给 general)。
        streaming: 是否声明 streaming 能力。
        provider: 可选 provider 信息(如 {"organization": ..., "url": ...})。
    """
    base = base_url.rstrip("/")
    supported_interfaces = [
        AgentInterface(
            url=f"{base}{JSONRPC_PATH}",
            protocol_binding="JSONRPC",
            protocol_version=A2A_PROTOCOL_VERSION,
        ),
        AgentInterface(
            url=f"{base}{REST_PATH_PREFIX}",
            protocol_binding="HTTP+JSON",
            protocol_version=A2A_PROTOCOL_VERSION,
        ),
    ]

    card_kwargs: dict = {
        "name": name,
        "description": description or f"{name} agent powered by ksadk",
        "version": version,
        "supported_interfaces": supported_interfaces,
        "capabilities": AgentCapabilities(streaming=streaming, push_notifications=False),
        "default_input_modes": list(_DEFAULT_IO_MODES),
        "default_output_modes": list(_DEFAULT_IO_MODES),
        "skills": _build_skills(skills),
    }
    if provider:
        from a2a.types import AgentProvider

        card_kwargs["provider"] = AgentProvider(
            organization=provider.get("organization", ""),
            url=provider.get("url", ""),
        )
    return AgentCard(**card_kwargs)


def _build_skills(skills: Sequence[str] | None) -> list[AgentSkill]:
    if not skills:
        return [
            AgentSkill(
                id="general",
                name="General",
                description="General purpose agent powered by ksadk",
                tags=["general"],
            )
        ]
    return [
        AgentSkill(
            id=skill,
            name=skill.replace("_", " ").title(),
            description=f"Skill: {skill}",
            tags=[skill],
        )
        for skill in skills
    ]


__all__ = [
    "A2A_PROTOCOL_VERSION",
    "JSONRPC_PATH",
    "REST_PATH_PREFIX",
    "build_agent_card",
]
