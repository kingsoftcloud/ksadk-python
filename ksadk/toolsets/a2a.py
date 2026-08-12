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

import difflib
import os
import uuid
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


def _discover_agents() -> list[dict[str, Any]]:
    """同步发现 space 下的可用 A2A agent（经 KOP ListAToASpaceAgents)。

    用同步 KOPClient,不用 async A2ASpaceClient——这样 tool 可在 langgraph 的
    async node 里安全调用(避免 sync 工具内部 asyncio.run 与运行中 event loop 冲突,
    也保证 OTel traceparent contextvar 沿 event loop 传递不断链)。
    """
    from ksadk.common.kop_client import KOPClient

    space_id = _a2a_space_id()
    if not space_id:
        return []
    try:
        kop = KOPClient()
        data = kop.post_action(
            "ListAToASpaceAgents",
            {"A2ASpaceId": space_id, "Status": "available", "PageSize": 100},
        )
    except Exception:
        return []
    result = []
    for item in data.get("Agents") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("InvocationStatus") or "") not in {"", "available"}:
            continue
        card = item.get("AgentCard") if isinstance(item.get("AgentCard"), dict) else {}
        ifaces = card.get("supportedInterfaces") or []
        card_url = str(ifaces[0].get("url")) if ifaces and isinstance(ifaces[0], dict) else ""
        result.append(
            {
                "agent_id": str(item.get("A2AAgentId") or ""),
                "name": str(card.get("name") or item.get("Name") or ""),
                "description": str(card.get("description") or item.get("Description") or ""),
                "version_id": str(item.get("VersionId") or item.get("LatestVersionId") or ""),
                "card_sha256": str(item.get("CardSha256") or ""),
                "source": str(item.get("Source") or "hosted"),
                "url": card_url,
            }
        )
    return result


def _match_agent(agents: list[dict[str, Any]], ref: str) -> tuple[dict[str, Any] | None, list[str]]:
    target = _normalize_agent_ref(ref)
    if not target:
        return None, [a["agent_id"] for a in agents]
    for a in agents:
        if _normalize_agent_ref(a["agent_id"]) == target or _normalize_agent_ref(
            a["name"]
        ) == target:
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
    """从 task 的 status.message.parts 与 artifacts 提取回复文本。

    兼容 proto Task 对象（属性访问）与 JSON dict（键访问）。
    """
    if isinstance(remote_task, dict):
        status = remote_task.get("status") or {}
        message = status.get("message") or {}
        parts: list[str] = []
        # 优先 status.message.parts（终态回复）；无则退到 artifacts 分片。
        for part in (message.get("parts") or []):
            if isinstance(part, dict) and part.get("text"):
                parts.append(part["text"])
        if parts:
            return "".join(parts)
        for artifact in (remote_task.get("artifacts") or []):
            for part in (artifact.get("parts") or []):
                if isinstance(part, dict) and part.get("text"):
                    parts.append(part["text"])
        return "".join(parts)
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
        agents = _discover_agents()
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
        agents = _discover_agents()
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
        agents = _discover_agents()
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
        import httpx

        card_url = str(matched.get("url") or "").strip()
        if not card_url:
            return {"ok": False, "error_message": f"agent {matched['agent_id']} card 无可用 url"}

        def _send() -> dict[str, Any]:
            # 直接对 card.url 发非流式 SendMessage(returnImmediately=false),同步拿终态回复。
            # 不走 space_client 的流式 send_message/subscribe(其 task 状态跟踪在异步下不可靠)。
            payload = {
                "jsonrpc": "2.0",
                "method": "SendMessage",
                "id": "1",
                "params": {
                    "message": {
                        "messageId": f"m-{uuid.uuid4().hex[:12]}",
                        "role": "ROLE_USER",
                        "parts": [{"text": message}],
                    },
                    "configuration": {"returnImmediately": False},
                },
            }
            # 透传 OTel trace context：把当前 span 注入 traceparent header,
            # 让被调 agent 的 span 挂到同一条分布式 trace 上。
            headers = {"Content-Type": "application/json", "A2A-Version": "1.0"}
            try:
                from opentelemetry import propagate

                propagate.inject(headers)
            except Exception:
                pass
            # SSRF 防护：拒绝内网/环回/链路本地地址（与 ksadk.toolsets.web 同款）。
            import ipaddress
            from urllib.parse import urlsplit

            _parsed = urlsplit(card_url)
            _host = (_parsed.hostname or "").lower()
            if _host and not _host.endswith(".ksyun.com"):
                try:
                    _ip = ipaddress.ip_address(_host)
                    if _ip.is_private or _ip.is_loopback or _ip.is_link_local or _ip.is_reserved:
                        return {
                            "ok": False,
                            "error_message": f"card url 指向内网地址，拒绝调用: {card_url}",
                        }
                except ValueError:
                    pass  # 非 IP hostname（如 agent-pre.kspmas.ksyun.com），放行
            resp = httpx.post(
                card_url,
                json=payload,
                headers=headers,
                timeout=120,
            )
            if resp.status_code != 200:
                return {
                    "ok": False,
                    "error_type": "A2AClientError",
                    "error_message": f"HTTP {resp.status_code}: {resp.text[:200]}",
                }
            data = resp.json()
            if "error" in data and data["error"]:
                err = str(data["error"])[:300]
                return {"ok": False, "error_type": "A2AError", "error_message": err}
            task = (data.get("result") or {}).get("task") or {}
            return {
                "ok": True,
                "agent": matched["agent_id"],
                "task_id": task.get("id", ""),
                "state": str((task.get("status") or {}).get("state", "")),
                "reply": _extract_reply_text(task),
            }

        return _send()
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}


def get_a2a_tools() -> list:
    return [as_tool(list_a2a_agents), as_tool(get_a2a_agent_card), as_tool(call_a2a_agent)]


_A2A_TOOL_POLICIES = {
    "list_a2a_agents": ToolPolicy(risk_level="low"),
    "get_a2a_agent_card": ToolPolicy(risk_level="low"),
    "call_a2a_agent": ToolPolicy(risk_level="high", side_effects=("a2a_remote_call",)),
}
