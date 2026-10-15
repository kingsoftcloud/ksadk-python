"""Model Profile connection diagnostics."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from ksadk.studio.contracts import ANTHROPIC_VERSION, ModelSpec, NetworkPolicy
from ksadk.studio.errors import StudioError

_ANTHROPIC_VERSION = ANTHROPIC_VERSION


async def test_model_profile_connection(
    *,
    catalog: Any,
    model_client: Any,
    resource_id: str,
) -> dict:
    descriptor = catalog.get(resource_id)
    if descriptor.kind != "model":
        raise StudioError(
            "RESOURCE_KIND_INVALID",
            "连接测试只能用于 Model Profile",
            status_code=422,
            details={"resourceId": resource_id},
        )
    spec = ModelSpec.model_validate(descriptor.contract)
    resolved = catalog.resolver.resolve_model(spec)
    resolved.parameters = resolved.parameters.model_copy(
        update={
            "temperature": 0,
            "max_tokens": min(resolved.parameters.max_tokens or 2048, 64),
        }
    )
    host = (urlparse(resolved.endpoint_url).hostname or "").lower().rstrip(".")
    started = time.monotonic()
    response = await model_client.complete(
        resolved,
        messages=[{"role": "user", "content": "这是连接测试。请只回复 OK。"}],
        network_policy=NetworkPolicy(
            mode="restricted",
            allowed_hosts=[host] if host else [],
            allow_private_network=False,
        ),
        timeout_seconds=20,
        max_attempts=1,
        backoff_seconds=0,
        allow_empty=True,
    )
    return {
        "ok": True,
        "resourceId": resource_id,
        "model": resolved.model,
        "finishReason": response.finish_reason,
        "latencyMs": int((time.monotonic() - started) * 1000),
    }


def _probe_candidates(raw_url: str) -> tuple[str, list[tuple[str, str]]]:
    """把用户粘贴的任意形态地址归一化为 (base_url, [(protocol, endpoint_url), ...])。

    支持三种输入形态：
    - https://host/v1/chat/completions → 直接候选
    - https://host/v1/responses → 直接候选
    - https://host/v1/messages → 直接候选（Anthropic Messages wire）
    - https://host 或 https://host/v1 → 推导 chat / responses / anthropic 候选
    """
    url = raw_url.strip().rstrip("/")
    if url.endswith("/chat/completions"):
        base = url[: -len("/chat/completions")]
        return base, [("chat", url)]
    if url.endswith("/responses"):
        base = url[: -len("/responses")]
        return base, [("responses", url)]
    if url.endswith("/messages"):
        base = url[: -len("/messages")]
        return base, [("anthropic", url)]
    if url.endswith("/v1"):
        return url, [
            ("chat", f"{url}/chat/completions"),
            ("responses", f"{url}/responses"),
            ("anthropic", f"{url}/messages"),
        ]
    return url, [
        ("chat", f"{url}/v1/chat/completions"),
        ("responses", f"{url}/v1/responses"),
        ("anthropic", f"{url}/v1/messages"),
        ("chat", f"{url}/chat/completions"),
        ("responses", f"{url}/responses"),
        ("anthropic", f"{url}/messages"),
    ]


def _classify_probe(status_code: int) -> str:
    if status_code == 200:
        return "ok"
    if status_code in {401, 403}:
        return "auth_required"
    if status_code in {400, 422, 429}:
        return "recognized"
    if status_code in {404, 405, 406}:
        return "unavailable"
    return "error"


def _recommended_probe(attempts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose the profile protocol from probe results deterministically.

    Responses is preferred for a generic URL when both endpoints are usable;
    an explicitly supplied endpoint still produces a single eligible result.
    """
    protocol_priority = {"responses": 0, "chat": 1, "anthropic": 2}
    for wanted in ("ok", "auth_required", "recognized"):
        eligible = [a for a in attempts if a.get("status") == wanted]
        hit = min(
            eligible,
            key=lambda attempt: protocol_priority.get(str(attempt.get("protocol")), 99),
            default=None,
        )
        if hit is not None:
            protocol = str(hit["protocol"])
            return {
                "protocol": protocol,
                "wireApi": protocol if protocol in {"responses", "anthropic"} else "chat",
                "endpointUrl": hit["endpointUrl"],
                "status": hit["status"],
            }
    return None


async def probe_model_endpoint(
    *,
    url: str,
    credential: str | None,
    network_guard: Any = None,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    """智能探测模型端点：识别 chat / responses / anthropic 协议、可用模型清单并给出推荐配置。"""
    base, candidates = _probe_candidates(url)
    host = (urlparse(base).hostname or "").lower().rstrip(".")
    headers = {"Content-Type": "application/json"}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"

    async def _post(
        client: httpx.AsyncClient,
        protocol: str,
        endpoint: str,
        probe_model: str,
    ) -> dict[str, Any]:
        if network_guard is not None:
            await network_guard.check(
                endpoint,
                NetworkPolicy(
                    mode="restricted",
                    allowed_hosts=[host] if host else [],
                    allow_private_network=False,
                ),
            )
        request_headers = dict(headers)
        payload: dict[str, Any]
        if protocol == "anthropic":
            request_headers["x-api-key"] = request_headers.pop("Authorization", "").removeprefix("Bearer ")
            request_headers["anthropic-version"] = _ANTHROPIC_VERSION
            payload = {
                "model": probe_model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 16,
            }
        elif protocol == "chat":
            payload = {
                "model": probe_model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "stream": False,
            }
        else:
            payload = {"model": probe_model, "input": "ping", "max_output_tokens": 16}
        started = time.monotonic()
        try:
            response = await client.post(endpoint, headers=request_headers, json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return {
                "protocol": protocol,
                "endpointUrl": endpoint,
                "status": "unreachable",
                "errorType": type(exc).__name__,
            }
        return {
            "protocol": protocol,
            "endpointUrl": endpoint,
            "status": _classify_probe(response.status_code),
            "httpStatus": response.status_code,
            "latencyMs": int((time.monotonic() - started) * 1000),
        }

    async def _models(client: httpx.AsyncClient) -> list[str]:
        models_urls = (
            (f"{base}/models", f"{base}/v1/models")
            if not base.endswith("/v1")
            else (f"{base}/models",)
        )
        # Anthropic Messages wire 用 x-api-key 鉴权，模型清单也用它重试一次；
        # OpenAI 兼容网关大多同时接受 Bearer，因此 Bearer 优先。
        auth_attempts = [headers]
        if credential:
            anthropic_headers = {
                key: value for key, value in headers.items() if key != "Authorization"
            }
            anthropic_headers["x-api-key"] = credential
            anthropic_headers["anthropic-version"] = _ANTHROPIC_VERSION
            auth_attempts.append(anthropic_headers)
        for models_url in models_urls:
            for attempt_headers in auth_attempts:
                try:
                    response = await client.get(models_url, headers=attempt_headers)
                except (httpx.TimeoutException, httpx.TransportError):
                    break
                if response.status_code != 200:
                    if response.status_code in {401, 403}:
                        continue
                    break
                try:
                    data = response.json()
                except ValueError:
                    continue
                items = data.get("data") or data.get("models") or []
                names = [
                    str(item.get("id") or item.get("name") or "")
                    for item in items
                    if isinstance(item, dict)
                ]
                names = [name for name in names if name]
                if names:
                    return sorted(set(names))[:100]
        return []

    timeout = httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        # 先拿模型清单：部分网关对不存在的模型名直接 403，用真实模型探测才能区分协议
        models = await _models(client)
        probe_model = models[0] if models else "__probe__"
        attempts = await asyncio.gather(
            *(_post(client, protocol, endpoint, probe_model) for protocol, endpoint in candidates)
        )

    recommended = _recommended_probe(attempts)

    return {
        "input": url,
        "baseUrl": base,
        "attempts": list(attempts),
        "recommended": recommended,
        "models": models,
    }


__all__ = ["test_model_profile_connection", "probe_model_endpoint"]
