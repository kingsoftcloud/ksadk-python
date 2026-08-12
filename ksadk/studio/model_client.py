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
    """Resolve immutable Secret references with a workspace-persisted overlay.

    Resolution order: in-memory session values → workspace secrets file
    (``.agentkit/secrets.env``) → process environment (with paired fallback).
    """

    _FALLBACK_PAIRS: tuple[tuple[str, str], ...] = (
        ("AGENTKIT_MODEL_API_KEY", "OPENAI_API_KEY"),
        ("OPENAI_API_KEY", "AGENTKIT_MODEL_API_KEY"),
    )

    def __init__(self, workspace: Any = None) -> None:
        self._session_values: dict[str, bytearray] = {}
        self._lock = RLock()
        self._workspace = workspace
        self._persisted: dict[str, str] | None = None

    def _secrets_path(self) -> Any | None:
        if self._workspace is None:
            return None
        try:
            return self._workspace.resolve(".agentkit/secrets.env")
        except Exception:
            return None

    def _load_persisted(self) -> dict[str, str]:
        if self._persisted is not None:
            return self._persisted
        self._persisted = {}
        path = self._secrets_path()
        if path is not None and path.is_file():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    if key:
                        self._persisted[key] = value
            except Exception:
                pass
        return self._persisted

    def _write_persisted(self) -> None:
        path = self._secrets_path()
        if path is None:
            return
        lines = [f"{k}={v}" for k, v in sorted(self._persisted.items())]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            try:
                path.chmod(0o600)
            except Exception:
                pass
        except Exception:
            pass

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
            if self._secrets_path() is not None:
                persisted = self._load_persisted()
                if persisted.get(name) != value:
                    persisted[name] = value
                    self._write_persisted()
        return self.status(f"env://{name}")

    def delete_session(self, name: str) -> dict[str, str | bool]:
        name = self._validate_name(name)
        with self._lock:
            previous = self._session_values.pop(name, None)
            self._zero(previous)
            if self._secrets_path() is not None:
                persisted = self._load_persisted()
                if name in persisted:
                    persisted.pop(name, None)
                    self._write_persisted()
        return self.status(f"env://{name}")

    def clear_session(self) -> None:
        with self._lock:
            values = list(self._session_values.values())
            self._session_values.clear()
            for value in values:
                self._zero(value)

    def _fallback_name(self, name: str) -> str | None:
        for primary, alternate in self._FALLBACK_PAIRS:
            if name == primary and alternate != primary:
                return alternate
        return None

    def _resolve_source(self, name: str) -> tuple[bool, str]:
        """Return (configured, source) considering session, persisted file, env, and fallback."""
        fallback = self._fallback_name(name)
        with self._lock:
            if name in self._session_values:
                return True, "session"
            if fallback and fallback in self._session_values:
                return True, "session-alias"
        persisted = self._load_persisted()
        if name in persisted:
            return True, "workspace"
        if fallback and fallback in persisted:
            return True, "workspace-alias"
        if os.environ.get(name):
            return True, "environment"
        if fallback and os.environ.get(fallback):
            return True, "fallback"
        return False, "missing"

    def _session_value(self, name: str) -> str | None:
        with self._lock:
            value = self._session_values.get(name)
            return value.decode("utf-8") if value is not None else None

    def status(self, reference: str) -> dict[str, str | bool]:
        name = self._environment_name(reference)
        configured, source = self._resolve_source(name)
        return {
            "reference": reference,
            "name": name,
            "configured": configured,
            "source": source,
            "persistence": source,
        }

    def resolve(self, reference: str) -> str:
        name = self._environment_name(reference)
        fallback = self._fallback_name(name)
        aliases = [name, *([fallback] if fallback else [])]
        for candidate in aliases:
            value = self._session_value(candidate)
            if value is not None:
                return value
        persisted = self._load_persisted()
        for candidate in aliases:
            if candidate in persisted:
                return persisted[candidate]
        for candidate in aliases:
            value = os.environ.get(candidate)
            if value:
                return value
        raise StudioError(
            "SECRET_NOT_FOUND",
            "模型凭证尚未配置",
            status_code=422,
            details={"reference": reference},
        )

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
        allow_empty: bool = False,
    ) -> ModelResponse:
        await self.network_guard.check(model.endpoint_url, network_policy)
        credential = self.credential_resolver.resolve(model.credential_ref)
        wire_api = (model.wire_api or "chat").strip().lower()
        if wire_api == "responses":
            payload = self._responses_payload(model, messages, tools)
        else:
            payload = {
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
                if wire_api == "responses":
                    return self._parse_responses_response(response, allow_empty=allow_empty)
                return self._parse_response(response, allow_empty=allow_empty)
        raise AssertionError("unreachable")

    @staticmethod
    def _responses_payload(
        model: ResolvedModel,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """把 chat 语义消息映射到 Responses API 最小可用载荷。"""
        if tools:
            raise StudioError(
                "MODEL_REQUEST_FAILED",
                "Responses 端点暂不支持 tools 参数",
                status_code=422,
            )
        instructions: list[str] = []
        items: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role") or "user")
            text = str(message.get("content") or "")
            if role == "system":
                instructions.append(text)
                continue
            part_type = "output_text" if role == "assistant" else "input_text"
            items.append({"role": role, "content": [{"type": part_type, "text": text}]})
        payload: dict[str, Any] = {
            "model": model.model,
            "input": items,
            "max_output_tokens": model.parameters.max_tokens,
        }
        if instructions:
            payload["instructions"] = "\n\n".join(instructions)
        return payload

    @staticmethod
    def _parse_responses_response(
        response: httpx.Response, allow_empty: bool = False
    ) -> ModelResponse:
        if len(response.content) > 16 * 1024 * 1024:
            raise StudioError(
                "MODEL_RESPONSE_TOO_LARGE",
                "模型响应超过 16 MiB 限制",
                status_code=502,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise StudioError(
                "MODEL_RESPONSE_INVALID",
                "模型响应不符合 Responses 协议",
                status_code=502,
            ) from exc
        content = payload.get("output_text")
        if not isinstance(content, str) or not content:
            parts: list[str] = []
            for item in payload.get("output") or []:
                if not isinstance(item, dict):
                    continue
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                        parts.append(str(part.get("text") or ""))
            content = "".join(parts)
        status = str(payload.get("status") or "")
        if not content and not allow_empty:
            raise StudioError(
                "MODEL_EMPTY_RESPONSE",
                "模型未返回可用内容",
                status_code=502,
                details={"status": status},
            )
        raw_usage = payload.get("usage") or {}
        usage = Usage(
            input_tokens=int(raw_usage.get("input_tokens") or 0),
            output_tokens=int(raw_usage.get("output_tokens") or 0),
            total_tokens=int(raw_usage.get("total_tokens") or 0),
            cached_input_tokens=int(
                (raw_usage.get("input_tokens_details") or {}).get("cached_tokens") or 0
            ),
            reasoning_output_tokens=int(
                (raw_usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0
            ),
        )
        return ModelResponse(
            content=content,
            finish_reason="stop" if status == "completed" else status,
            usage=usage,
            tool_calls=[],
            raw_message={"output": payload.get("output")},
        )

    @staticmethod
    def _parse_response(response: httpx.Response, allow_empty: bool = False) -> ModelResponse:
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
        if not content and not calls and not allow_empty:
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
                (raw_usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
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
