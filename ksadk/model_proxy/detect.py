"""模型能力矩阵探测(v2.3):替代简单 responses/chat 二分。

"支持 responses" 不等于 "该直通"——glm-5.1 原生 responses 丢增量 delta + 拒 namespace 工具,
经转换层反而更好。本模块用能力矩阵描述一个 (model, base_url, credential) 的真实能力,
供路由决策(直通 responses / 走转换层 chat / …)。

探测铁律(review v1):
- **功能性 probe 而非状态码 probe**:发最小真请求打 /v1/responses,校验返回是否为
  合法 responses 结构(有 ``output``/``status``),不只看 HTTP 200——网关对不支持的
  端点有时也返回 200 但内容是错误页。
- **只在 "确凿协议否定" 时翻案**:`404/405/400 unknown endpoint` 才判 "不支持 responses";
  超时/5xx/429/401/403 一律 "unknown",不改变判定(故障 ≠ 能力缺失)。
- ``stream_delta_ok`` 需真发流式请求才能判定,成本高,本模块默认 None(不探),
  由调用方按需触发或默认走转换层(转换层自己生成完整 delta)。
- Codex 直连还需功能性 tool probes:真实发送当前 Codex 会声明的
  ``additional_tools`` 工具面。任一必需类型被拒时保留
  ``responses_supported=True``,同时推荐经 Chat 转换层执行。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

Verdict = Literal["supported", "unsupported", "unknown"]

# These are Codex wire-contract capabilities, not provider/model allowlists.
# namespace/custom are required for a native Responses turn; web_search is an
# optional built-in that may be disabled without downgrading the whole protocol.
CODEX_DIRECT_REQUIRED_TOOL_TYPES = frozenset({"namespace", "custom"})
CODEX_OPTIONAL_TOOL_TYPES = frozenset({"web_search"})


@dataclass
class ModelCapabilities:
    """一个 (model, base_url, credential_scope) 的协议能力矩阵。"""

    responses_supported: bool | None = None  # None = 未知(未探/不确定)
    stream_delta_ok: bool | None = None  # 流式增量 delta;None = 未探
    tool_types: set[str] = field(default_factory=set)  # 已实测支持的 Codex 工具类型
    preferred_protocol: str = "chat"  # "responses" | "chat":路由该走哪条
    checked_at: float = 0.0
    verdict: Verdict = "unknown"  # 最近一次探测结论


def _classify_responses_error(status: int, body: str) -> Verdict:
    """把非 200 响应分类成 supported/unsupported/unknown。

    只有 "确凿协议否定" 才判 unsupported;故障类一律 unknown(不误固化)。
    """
    if status in (404, 405):
        return "unsupported"
    if status == 400:
        low = (body or "").lower()
        # 400 且 body 含 "unknown/not supported/endpoint" -> endpoint 不支持
        if any(k in low for k in ("unknown", "not supported", "endpoint", "unrecognized")):
            return "unsupported"
        return "unknown"  # 400 但不像协议级否定(可能是请求字段问题)
    # 401/403:鉴权/权限问题,不代表 endpoint 不存在(同 key 不同能力,见 glm-5.1 403)
    if status in (401, 403):
        return "unknown"
    return "unknown"  # 5xx/429/其他:故障类


def probe_responses_capability(
    client: httpx.Client | httpx.AsyncClient,
    base: str,
    key: str,
    model: str,
    timeout: float = 30.0,
):
    """功能性 probe /v1/responses,返回能力矩阵(sync) 或 coroutine(async)。

    - 纯文本 200 且结构合法后，再逐个探测真实 Codex 工具类型
    - namespace/custom 成功→ preferred_protocol=responses
    - 仅 web_search 被拒→仍为 responses，但不把 web_search 记入 tool_types
    - namespace/custom 被拒→ supported,preferred_protocol=chat
    - 200 但非 responses 结构(网关伪 200)→ unsupported,preferred_protocol=chat
    - 404/405/400 unknown → unsupported,preferred_protocol=chat
    - 超时/5xx/429/401/403 → unknown,preferred_protocol 保持默认 chat(保守走转换层)

    按 client 类型分发:``httpx.Client`` 同步返回 ``ModelCapabilities``;
    ``httpx.AsyncClient`` 返回 coroutine(调用方 await)。
    """
    if isinstance(client, httpx.AsyncClient):
        return _probe_async(client, base, key, model, timeout)
    if isinstance(client, httpx.Client):
        return _probe_sync(client, base, key, model, timeout)
    raise TypeError(f"client 必须是 httpx.Client/AsyncClient,不是 {type(client).__name__}")


def _probe_sync(
    client: httpx.Client, base: str, key: str, model: str, timeout: float
) -> ModelCapabilities:
    caps = ModelCapabilities(checked_at=time.time(), verdict="unknown")
    url = f"{base.rstrip('/')}/responses"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        r = client.post(
            url,
            json=_base_probe_payload(model),
            headers=headers,
            timeout=timeout,
        )
    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError):
        return _finalize(caps)
    try:
        data = r.json()
    except ValueError:
        data = None
    caps = _apply_response(caps, r.status_code, r.text, data)
    if not caps.responses_supported:
        return caps
    for tool_type, payload in _codex_tool_probe_payloads(model):
        try:
            tool_response = client.post(
                url,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError):
            caps.verdict = "unknown"
            return _finalize(caps)
        try:
            tool_data = tool_response.json()
        except ValueError:
            tool_data = None
        caps = _apply_tool_response(
            caps,
            tool_type,
            tool_response.status_code,
            tool_response.text,
            tool_data,
        )
        if tool_type not in caps.tool_types:
            return caps
    return _finalize(caps)


async def _probe_async(
    client: httpx.AsyncClient, base: str, key: str, model: str, timeout: float
) -> ModelCapabilities:
    caps = ModelCapabilities(checked_at=time.time(), verdict="unknown")
    url = f"{base.rstrip('/')}/responses"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        r = await client.post(
            url,
            json=_base_probe_payload(model),
            headers=headers,
            timeout=timeout,
        )
    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError):
        return _finalize(caps)
    try:
        data = r.json()
    except ValueError:
        data = None
    caps = _apply_response(caps, r.status_code, r.text, data)
    if not caps.responses_supported:
        return caps
    for tool_type, payload in _codex_tool_probe_payloads(model):
        try:
            tool_response = await client.post(
                url,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError):
            caps.verdict = "unknown"
            return _finalize(caps)
        try:
            tool_data = tool_response.json()
        except ValueError:
            tool_data = None
        caps = _apply_tool_response(
            caps,
            tool_type,
            tool_response.status_code,
            tool_response.text,
            tool_data,
        )
        if tool_type not in caps.tool_types:
            return caps
    return _finalize(caps)


def _base_probe_payload(model: str) -> dict[str, Any]:
    return {"model": model, "input": "hi", "max_output_tokens": 1, "stream": False}


def _namespace_probe_payload(model: str) -> dict[str, Any]:
    """Return the smallest real Codex 0.147 dynamic-tool declaration."""

    return {
        "model": model,
        "input": [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "functions",
                        "description": "KsADK Codex capability probe",
                        "tools": [
                            {
                                "type": "function",
                                "name": "probe",
                                "description": "Probe Codex namespace tool support",
                                "parameters": {
                                    "type": "object",
                                    "properties": {},
                                    "additionalProperties": False,
                                },
                            }
                        ],
                    }
                ],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Reply with OK."}],
            },
        ],
        "max_output_tokens": 1,
        "stream": False,
    }


def _custom_probe_payload(model: str) -> dict[str, Any]:
    """Return the smallest Codex namespaced freeform-tool declaration."""

    payload = _namespace_probe_payload(model)
    payload["input"][0]["tools"][0]["tools"] = [
        {
            "type": "custom",
            "name": "probe",
            "description": "Probe Codex custom tool support",
            "format": {
                "type": "grammar",
                "syntax": "lark",
                "definition": 'start: "OK"',
            },
        }
    ]
    return payload


def _web_search_probe_payload(model: str) -> dict[str, Any]:
    """Return Codex's ``additional_tools`` built-in web-search declaration."""

    payload = _namespace_probe_payload(model)
    payload["input"][0]["tools"] = [{"type": "web_search"}]
    return payload


def _codex_tool_probe_payloads(model: str) -> tuple[tuple[str, dict[str, Any]], ...]:
    """Return the model-agnostic direct tool surface required by Codex."""

    return (
        ("namespace", _namespace_probe_payload(model)),
        ("custom", _custom_probe_payload(model)),
        ("web_search", _web_search_probe_payload(model)),
    )


def _apply_tool_response(
    caps: ModelCapabilities, tool_type: str, status: int, text: str, data: Any
) -> ModelCapabilities:
    valid_envelope = isinstance(data, dict) and "output" in data and "status" in data
    envelope_failed = valid_envelope and (
        str(data.get("status") or "").lower() == "failed" or bool(data.get("error"))
    )
    if status == 200 and valid_envelope and not envelope_failed:
        caps.tool_types.add(tool_type)
        caps.verdict = "supported"
        return _finalize(caps)

    low = (text or "").lower()
    dialect_marker = any(
        marker in low
        for marker in (
            tool_type.lower(),
            "additional_tools",
            "invalid value",
            "supported values",
            "not supported",
            "unrecognized",
        )
    )
    dialect_rejected = (
        status in (400, 404, 405, 422) and (status in (404, 405) or dialect_marker)
    ) or (status == 200 and envelope_failed and dialect_marker)
    if not dialect_rejected:
        # Transient failures do not establish direct compatibility and should
        # not be cached as a native Codex tool-capable endpoint.
        caps.verdict = "unknown"
    return _finalize(caps)


def _apply_response(
    caps: ModelCapabilities, status: int, text: str, data: Any
) -> ModelCapabilities:
    if status == 200:
        # 功能性校验:合法 responses 结构有 output + status(不只看 200)
        if isinstance(data, dict) and "output" in data and "status" in data:
            caps.responses_supported = True
            caps.verdict = "supported"
        else:
            # 200 但不是 responses 结构(网关伪 200 / 错误页)
            caps.responses_supported = False
            caps.verdict = "unsupported"
        return _finalize(caps)
    caps.verdict = _classify_responses_error(status, text)
    caps.responses_supported = False if caps.verdict == "unsupported" else None
    return _finalize(caps)


def _finalize(caps: ModelCapabilities) -> ModelCapabilities:
    """根据 verdict 定 preferred_protocol(保守:不确定也走转换层 chat)。"""
    if (
        caps.verdict == "supported"
        and caps.responses_supported
        and CODEX_DIRECT_REQUIRED_TOOL_TYPES.issubset(caps.tool_types)
    ):
        caps.preferred_protocol = "responses"
    else:
        # unsupported 或 unknown 都默认 chat(走转换层):unknown 时走转换层更安全,
        # 因为转换层对 chat 模型是确定可用的,而直连 responses 在 unknown 下有风险
        caps.preferred_protocol = "chat"
    return caps
