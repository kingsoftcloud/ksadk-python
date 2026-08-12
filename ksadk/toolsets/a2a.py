"""A2A toolset: 把 A2A Space 下的远程 agent 暴露为固定几个元 tool（对齐 skill 模式）。

不随 agent 数量增长：固定 3 个 tool —— list_a2a_agents / get_a2a_agent_card /
call_a2a_agent。LLM 操作哪个远程 agent 通过参数动态指定。

容错（参考 ksadk/toolsets/skills.py 并加强误传容错）：
- agent 参数同时接受 A2AAgentId 或 AgentCard.name，内部归一化（strip/lower、
  容忍 a2a-agent- 前缀有无）；
- 找不到时返回 available_agents + difflib 模糊建议；
- 统一 {ok, error_type, error_message} 信封，永不抛异常；
- 未绑 A2A Space 时挂载 0 个 tool（空值守卫）。
"""

from __future__ import annotations

import asyncio
import difflib
import os
from typing import Any

from ksadk.tools.gateway import ToolPolicy
from ksadk.toolsets._langchain import as_tool

ENV_A2A_SPACE_ID = "KSADK_A2A_SPACE_ID"
ENV_A2A_SPACE_IDS = "KSADK_A2A_SPACE_IDS"


def _a2a_space_id() -> str:
    explicit = (os.getenv(ENV_A2A_SPACE_ID) or "").strip()
    if explicit:
        return explicit
    raw = (os.getenv(ENV_A2A_SPACE_IDS) or "").strip()
    if not raw:
        return ""
    try:
        import json

        ids = json.loads(raw)
        if isinstance(ids, list) and ids and isinstance(ids[0], str):
            return ids[0].strip()
    except (ValueError, IndexError):
        pass
    return ""


def _normalize_agent_ref(value: str) -> str:
    return (value or "").strip().lower().removeprefix("a2a-agent-")


async def _discover_agents() -> list[dict[str, Any]]:
    from ksadk.a2a.space_client import A2ASpaceClient

    space_id = _a2a_space_id()
    if not space_id:
        return []
    try:
        async with A2ASpaceClient.from_env(space_id=space_id) as client:
            agents = await client.discover()
    except Exception:
        return []
    result = []
    for a in agents:
        card = a.agent_card
        name = str(getattr(card, "name", "") or "")
        result.append(
            {
                "agent_id": a.agent_id,
                "name": name,
                "description": str(getattr(card, "description", "") or ""),
                "version_id": a.version_id,
                "card_sha256": a.card_sha256,
                "source": a.source,
            }
        )
    return result


def _match_agent(agents: list[dict[str, Any]], ref: str) -> tuple[dict[str, Any] | None, list[str]]:
    target = _normalize_agent_ref(ref)
    if not target:
        return None, [a["agent_id"] for a in agents]
    for a in agents:
        if _normalize_agent_ref(a["agent_id"]) == target or _normalize_agent_ref(a["name"]) == target:
            return a, []
    available = [a["agent_id"] for a in agents]
    names = [_normalize_agent_ref(a["agent_id"]) for a in agents] + [
        _normalize_agent_ref(a["name"]) for a in agents if a["name"]
    ]
    suggestions = difflib.get_close_matches(target, names, n=1, cutoff=0.6)
    return None, available + (suggestions or [])


def _task_state_name(remote_task: Any) -> str:
    status = getattr(remote_task, "status", None)
    state = getattr(status, "state", None)
    return str(state) if state is not None else ""


def _extract_reply_text(remote_task: Any) -> str:
    """从 remote task 的 status.message.parts 与 artifacts 提取回复文本。"""
    parts_text: list[str] = []
    status = getattr(remote_task, "status", None)
    message = getattr(status, "message", None)
    if message is not None:
        for part in (getattr(message, "parts", None) or []):
            text = getattr(part, "text", None)
            if isinstance(text, str) and text:
                parts_text.append(text)
    for artifact in (getattr(remote_task, "artifacts", None) or []):
        for part in (getattr(artifact, "parts", None) or []):
            text = getattr(part, "text", None)
            if isinstance(text, str) and text:
                parts_text.append(text)
    return "".join(parts_text)


def list_a2a_agents() -> dict[str, Any]:
    """List remote A2A agents in the configured A2A Space.

    Returns each agent's id, name, description and version. No remote calls
    are made to the agents themselves — only discovery metadata.
    """
    space_id = _a2a_space_id()
    if not space_id:
        return {"ok": False, "error_message": "A2A Space not configured"}
    try:
        agents = asyncio.run(_discover_agents())
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}
    return {"ok": True, "space_id": space_id, "agents": agents}


def get_a2a_agent_card(agent: str) -> dict[str, Any]:
    """Get the AgentCard of a remote A2A agent by id or name.

    ``agent`` accepts the A2AAgentId (a2a-agent-*) or the card name. When not
    found, returns available_agents and a suggestion.
    """
    space_id = _a2a_space_id()
    if not space_id:
        return {"ok": False, "error_message": "A2A Space not configured"}
    try:
        agents = asyncio.run(_discover_agents())
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}
    matched, hint = _match_agent(agents, agent)
    if matched is None:
        return {
            "ok": False,
            "error_message": f"Agent not found: {agent}",
            "available_agents": hint,
        }
    return {"ok": True, "agent": matched}


def call_a2a_agent(agent: str, message: str) -> dict[str, Any]:
    """Call a remote A2A agent by id or name with a text message.

    ``agent`` accepts A2AAgentId (a2a-agent-*) or card name. Synchronously waits
    for the remote task to complete and returns the agent's text reply. If the
    task does not reach a terminal state within the poll budget, returns the
    last known state and task_id for follow-up.
    """
    space_id = _a2a_space_id()
    if not space_id:
        return {"ok": False, "error_message": "A2A Space not configured"}
    if not (message or "").strip():
        return {"ok": False, "error_message": "message is required"}
    try:
        agents = asyncio.run(_discover_agents())
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}
    matched, hint = _match_agent(agents, agent)
    if matched is None:
        return {
            "ok": False,
            "error_message": f"Agent not found: {agent}",
            "available_agents": hint,
        }
    try:
        from ksadk.a2a.space_client import A2ASpaceClient

        async def _call() -> dict[str, Any]:
            import asyncio as _aio

            async with A2ASpaceClient.from_env(space_id=space_id) as client:
                task = await client.send_message(matched["agent_id"], message)
                platform_task_id = task.id
                remote_task = task.remote_task
                # send_message 返回首个 task(SUBMITTED);轮询 get_task 到终态拿回复文本。
                for _ in range(60):
                    state = _task_state_name(remote_task)
                    if state in {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}:
                        break
                    await _aio.sleep(1)
                    try:
                        polled = await client.get_task(platform_task_id)
                        remote_task = polled.remote_task
                    except Exception:
                        break
                return {
                    "task_id": platform_task_id,
                    "remote_task_id": str(getattr(remote_task, "id", "") or ""),
                    "state": _task_state_name(remote_task),
                    "reply": _extract_reply_text(remote_task),
                }

        result = asyncio.run(_call())
        return {"ok": True, "agent": matched["agent_id"], **result}
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}


def get_a2a_tools() -> list:
    return [as_tool(list_a2a_agents), as_tool(get_a2a_agent_card), as_tool(call_a2a_agent)]


_A2A_TOOL_POLICIES = {
    "list_a2a_agents": ToolPolicy(risk_level="low"),
    "get_a2a_agent_card": ToolPolicy(risk_level="low"),
    "call_a2a_agent": ToolPolicy(risk_level="high", side_effects=("a2a_remote_call",)),
}
