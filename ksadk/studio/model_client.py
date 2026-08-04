"""Safe OpenAI-compatible model client for local Studio runs."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx

from ksadk.studio.contracts import NetworkPolicy, ResolvedModel, Usage
from ksadk.studio.errors import StudioError

_DENIED_METADATA_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
    "metadata.azure.internal",
    "fd00:ec2::254",
}


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    content: str
    finish_reason: str
    usage: Usage
    tool_calls: list[ToolCall]
    raw_message: dict[str, Any]


class CredentialResolver:
    """Resolve immutable Secret references with an ephemeral local-session overlay."""

    def __init__(self) -> None:
        self._session_values: dict[str, bytearray] = {}
        self._lock = RLock()

    @staticmethod
    def _validate_name(name: str) -> str:
        allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
        if not name or any(char not in allowed for char in name):
            raise StudioError(
                "SECRET_REFERENCE_INVALID",
                "环境变量 Secret 引用格式无效",
                status_code=422,
                field="credentialName",
            )
        return name

    @classmethod
    def _environment_name(cls, reference: str) -> str:
        if not reference.startswith("env://"):
            raise StudioError(
                "SECRET_BACKEND_UNAVAILABLE",
                "当前本地 Runtime 仅支持 env:// Secret 引用",
                status_code=501,
                details={"scheme": reference.partition("://")[0]},
            )
        return cls._validate_name(reference.removeprefix("env://"))

    @staticmethod
    def _zero(value: bytearray | None) -> None:
        if value is not None:
            value[:] = b"\x00" * len(value)

    def put_session(self, name: str, value: str) -> dict[str, str | bool]:
        name = self._validate_name(name)
        if (
            not value
            or len(value) > 16_384
            or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise StudioError(
                "SECRET_VALUE_INVALID",
                "凭证不能为空、包含控制字符或超过 16 KiB",
                status_code=422,
                field="value",
            )
        encoded = bytearray(value.encode("utf-8"))
        with self._lock:
            previous = self._session_values.get(name)
            self._session_values[name] = encoded
            self._zero(previous)
        return self.status(f"env://{name}")

    def delete_session(self, name: str) -> dict[str, str | bool]:
        name = self._validate_name(name)
        with self._lock:
            previous = self._session_values.pop(name, None)
            self._zero(previous)
        return self.status(f"env://{name}")

    def clear_session(self) -> None:
        with self._lock:
            values = list(self._session_values.values())
            self._session_values.clear()
            for value in values:
                self._zero(value)

    def status(self, reference: str) -> dict[str, str | bool]:
        name = self._environment_name(reference)
        with self._lock:
            session_configured = name in self._session_values
        environment_configured = bool(os.environ.get(name))
        source = (
            "session"
            if session_configured
            else "environment"
            if environment_configured
            else "missing"
        )
        return {
            "reference": reference,
            "name": name,
            "configured": session_configured or environment_configured,
            "source": source,
            "persistence": "session" if session_configured else source,
        }

    def resolve(self, reference: str) -> str:
        name = self._environment_name(reference)
        with self._lock:
            session_value = self._session_values.get(name)
            if session_value is not None:
                return session_value.decode("utf-8")
        value = os.environ.get(name)
        if not value:
            raise StudioError(
                "SECRET_NOT_FOUND",
                "模型凭证尚未配置",
                status_code=422,
                details={"reference": reference},
            )
        return value

    def exists(self, reference: str) -> bool:
        try:
            return bool(self.status(reference)["configured"])
        except StudioError:
            return False


class NetworkGuard:
    async def check(self, endpoint_url: str, policy: NetworkPolicy) -> None:
        parsed = urlparse(endpoint_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not host:
            raise StudioError(
                "MODEL_ENDPOINT_INVALID",
                "模型地址必须是有效的 HTTP(S) URL",
                status_code=422,
            )
        allowed = {value.lower().rstrip(".") for value in policy.allowed_hosts}
        if policy.mode == "restricted" and host not in allowed:
            raise StudioError(
                "NETWORK_TARGET_DENIED",
                "目标 hostname 不在网络允许清单中",
                status_code=403,
                details={"host": host},
            )
        if host in _DENIED_METADATA_HOSTS:
            raise self._denied(host)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            records = await asyncio.to_thread(
                socket.getaddrinfo,
                host,
                port,
                0,
                socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise StudioError(
                "NETWORK_TARGET_UNRESOLVED",
                "模型 hostname 无法解析",
                status_code=422,
                details={"host": host},
            ) from exc
        for record in records:
            address = str(record[4][0]).split("%", 1)[0]
            ip = ipaddress.ip_address(address)
            if str(ip) in _DENIED_METADATA_HOSTS or ip.is_link_local or ip.is_multicast:
                raise self._denied(host)
            if (
                ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_unspecified
            ) and not policy.allow_private_network:
                raise self._denied(host)

    @staticmethod
    def _denied(host: str) -> StudioError:
        return StudioError(
            "NETWORK_TARGET_DENIED",
            "目标地址不满足本地 Runtime 网络策略",
            status_code=403,
            details={"host": host},
        )


class OpenAICompatibleModelClient:
    def __init__(
        self,
        *,
        credential_resolver: CredentialResolver | None = None,
        network_guard: NetworkGuard | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.credential_resolver = credential_resolver or CredentialResolver()
        self.network_guard = network_guard or NetworkGuard()
        self.transport = transport
        self.sleep = sleep

    async def complete(
        self,
        model: ResolvedModel,
        *,
        messages: list[dict[str, Any]],
        network_policy: NetworkPolicy,
        timeout_seconds: int,
        max_attempts: int,
        backoff_seconds: float,
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        await self.network_guard.check(model.endpoint_url, network_policy)
        credential = self.credential_resolver.resolve(model.credential_ref)
        payload: dict[str, Any] = {
            "model": model.model,
            "messages": messages,
            "temperature": model.parameters.temperature,
            "max_tokens": model.parameters.max_tokens,
            "stream": False,
        }
        if model.parameters.top_p is not None:
            payload["top_p"] = model.parameters.top_p
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(timeout_seconds, connect=min(10, timeout_seconds))
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=timeout,
            follow_redirects=False,
        ) as client:
            for attempt in range(1, max_attempts + 1):
                try:
                    response = await client.post(
                        model.endpoint_url,
                        headers=headers,
                        json=payload,
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt >= max_attempts:
                        raise StudioError(
                            "MODEL_REQUEST_FAILED",
                            "模型请求网络失败",
                            status_code=502,
                            details={"attempts": attempt, "errorType": type(exc).__name__},
                        ) from exc
                    await self.sleep(backoff_seconds * attempt)
                    continue
                if response.is_redirect:
                    raise StudioError(
                        "NETWORK_TARGET_DENIED",
                        "模型 endpoint 不允许重定向",
                        status_code=403,
                        details={"statusCode": response.status_code},
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < max_attempts:
                        await self.sleep(backoff_seconds * attempt)
                        continue
                if response.status_code >= 400:
                    raise StudioError(
                        "MODEL_REQUEST_FAILED",
                        "模型服务返回错误",
                        status_code=502,
                        details={"upstreamStatus": response.status_code},
                    )
                return self._parse_response(response)
        raise AssertionError("unreachable")

    @staticmethod
    def _parse_response(response: httpx.Response) -> ModelResponse:
        if len(response.content) > 16 * 1024 * 1024:
            raise StudioError(
                "MODEL_RESPONSE_TOO_LARGE",
                "模型响应超过 16 MiB 限制",
                status_code=502,
            )
        try:
            payload = response.json()
            choice = payload["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise StudioError(
                "MODEL_RESPONSE_INVALID",
                "模型响应不符合 OpenAI-compatible 协议",
                status_code=502,
            ) from exc
        calls = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            calls.append(
                ToolCall(
                    id=str(raw.get("id") or ""),
                    name=str(function.get("name") or ""),
                    arguments=str(function.get("arguments") or "{}"),
                    raw=raw,
                )
            )
        content = str(message.get("content") or "")
        finish_reason = str(choice.get("finish_reason") or "")
        if not content and not calls:
            raise StudioError(
                "MODEL_EMPTY_RESPONSE",
                "模型未返回可用内容",
                status_code=502,
                details={"finishReason": finish_reason},
            )
        raw_usage = payload.get("usage") or {}
        usage = Usage(
            input_tokens=int(raw_usage.get("prompt_tokens") or 0),
            output_tokens=int(raw_usage.get("completion_tokens") or 0),
            total_tokens=int(raw_usage.get("total_tokens") or 0),
            cached_input_tokens=int(
                (raw_usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            ),
            reasoning_output_tokens=int(
                (raw_usage.get("completion_tokens_details") or {}).get(
                    "reasoning_tokens"
                )
                or 0
            ),
            reported=bool(raw_usage),
            source="model-provider" if raw_usage else None,
        )
        return ModelResponse(
            content=content,
            finish_reason=finish_reason,
            usage=usage,
            tool_calls=calls,
            raw_message=message,
        )
