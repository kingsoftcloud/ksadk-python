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
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

Verdict = Literal["supported", "unsupported", "unknown"]


@dataclass
class ModelCapabilities:
    """一个 (model, base_url, credential_scope) 的协议能力矩阵。"""

    responses_supported: bool | None = None  # None = 未知(未探/不确定)
    stream_delta_ok: bool | None = None  # 流式增量 delta;None = 未探
    tool_types: set[str] = field(default_factory=set)  # 支持的工具 namespace
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

    - 200 且结构合法(output+status)→ supported,preferred_protocol=responses
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
    payload = {"model": model, "input": "hi", "max_output_tokens": 1, "stream": False}
    try:
        r = client.post(url, json=payload, headers=headers, timeout=timeout)
    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError):
        return _finalize(caps)
    try:
        data = r.json()
    except ValueError:
        data = None
    return _apply_response(caps, r.status_code, r.text, data)


async def _probe_async(
    client: httpx.AsyncClient, base: str, key: str, model: str, timeout: float
) -> ModelCapabilities:
    caps = ModelCapabilities(checked_at=time.time(), verdict="unknown")
    url = f"{base.rstrip('/')}/responses"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {"model": model, "input": "hi", "max_output_tokens": 1, "stream": False}
    try:
        r = await client.post(url, json=payload, headers=headers, timeout=timeout)
    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError):
        return _finalize(caps)
    try:
        data = r.json()
    except ValueError:
        data = None
    return _apply_response(caps, r.status_code, r.text, data)


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
    if caps.verdict == "supported" and caps.responses_supported:
        caps.preferred_protocol = "responses"
    else:
        # unsupported 或 unknown 都默认 chat(走转换层):unknown 时走转换层更安全,
        # 因为转换层对 chat 模型是确定可用的,而直连 responses 在 unknown 下有风险
        caps.preferred_protocol = "chat"
    return caps
