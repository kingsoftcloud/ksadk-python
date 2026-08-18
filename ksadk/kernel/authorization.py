# -*- coding: utf-8 -*-
"""AgentControlPermit 验证（Phase 1 Task 6 Step 4）。

- 签名：Ed25519，输入为除 ``signature`` 外、key-sort、无空白 UTF-8 JSON；
  时间戳归一化为 UTC RFC3339 秒精度；签名为 base64url 无 padding。
- key 获取：JWKS 源 + 进程内缓存（明确 max-age）；未知 key 只刷新一次，
  刷新后仍缺失则 fail closed。
- claims：operation 越权、tenant/agent_instance/session 绑定不符、mutation
  nonce 复用（仅允许同一 command/idempotency_key 的网络重试）一律拒绝。
- permit 过期抛 :class:`PermitExpiredError`，由 facade 决定是否允许 duplicate。

验证成功只向调用方暴露 ``permit_id``、``subject_ref``、``claims_digest``，
不回传 permit 原文或密钥材料。
"""
from __future__ import annotations

import base64
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from ksadk.kernel.contracts import AgentControlPermit
from ksadk.kernel.errors import AgentKernelError, InvalidPermitError

MUTATION_OPERATIONS = frozenset(
    {
        "enqueue", "steer", "inject", "interrupt",
        "pause", "resume", "submit_interaction",
    }
)
READ_OPERATIONS = frozenset({"get_status", "subscribe_events"})

# permit 有效期上限（与 server ``PERMIT_MAX_TTL_SECONDS`` 对齐）。
PERMIT_MAX_TTL_SECONDS = 300.0

_TIMESTAMP_FIELDS = ("issued_at", "expires_at")


@runtime_checkable
class NonceStore(Protocol):
    """mutation nonce 单次使用存储。

    默认进程内实现只覆盖单 Pod；跨 Pod / 重启的 durable 语义由注入的
    持久化实现提供（见 ``PostgresNonceStore``）。返回 True 表示记录成功
    或同一 ``(command_id, idempotency_key)`` 的网络重试；False 表示同
    nonce 被其它 command 复用（重放）。
    """

    async def register(
        self, nonce: str, command_id: str, idempotency_key: str
    ) -> bool: ...


class InMemoryNonceStore:
    """进程内默认实现（单 Pod；测试与本地运行）。"""

    def __init__(self) -> None:
        self._nonces: dict[str, tuple[str, str]] = {}

    async def register(
        self, nonce: str, command_id: str, idempotency_key: str
    ) -> bool:
        prior = self._nonces.get(nonce)
        if prior is not None and prior != (command_id, idempotency_key):
            return False
        self._nonces[nonce] = (command_id, idempotency_key)
        return True


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def normalize_rfc3339_seconds(value: str) -> str:
    """UTC RFC3339 秒精度（无毫秒、无偏移）。"""

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_rfc3339(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def canonical_permit_bytes(permit: AgentControlPermit) -> bytes:
    """签名输入：除 signature 外 key-sort 无空白 UTF-8 JSON。"""

    dump = permit.model_dump(mode="json", exclude={"signature"})
    for field in _TIMESTAMP_FIELDS:
        dump[field] = normalize_rfc3339_seconds(dump[field])
    return json.dumps(
        dump, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sign_permit(permit: AgentControlPermit, private_key: Ed25519PrivateKey) -> str:
    """签发方 helper（server / 测试使用；SDK 运行时只验签）。"""

    return b64url_encode(private_key.sign(canonical_permit_bytes(permit)))


@runtime_checkable
class JwksSource(Protocol):
    async def fetch_verification_keys(self) -> Mapping[str, str]:
        """key_id -> base64url(raw Ed25519 public key)。"""
        ...


@dataclass(frozen=True)
class VerifiedAdmission:
    """验证成功后唯一允许进入 Store 的 permit 事实（引用与摘要）。"""

    permit_id: str
    subject_ref: str
    claims_digest: str
    key_id: str
    operation: str


class PermitExpiredError(AgentKernelError):
    """permit 已过期；wire code 复用 ``invalid_permit``，分支语义独立。"""

    def __init__(self, message: str = "permit_expired", **kwargs) -> None:
        AgentKernelError.__init__(self, "invalid_permit", message, retryable=False, **kwargs)


class AgentControlPermitVerifier:
    def __init__(
        self,
        jwks: JwksSource,
        *,
        cache_max_age_seconds: float = 300.0,
        monotonic=time.monotonic,
        nonce_store: NonceStore | None = None,
    ) -> None:
        self._jwks = jwks
        self._cache_max_age = float(cache_max_age_seconds)
        self._monotonic = monotonic
        self._keys: dict[str, Ed25519PublicKey] = {}
        self._fetched_at = float("-inf")
        # nonce 单次使用：默认进程内，durable 语义注入 NonceStore。
        self._nonce_store: NonceStore = nonce_store or InMemoryNonceStore()

    async def _verification_key(self, key_id: str) -> Ed25519PublicKey:
        if key_id in self._keys and self._monotonic() - self._fetched_at < self._cache_max_age:
            return self._keys[key_id]
        raw = await self._jwks.fetch_verification_keys()
        self._keys = {
            kid: Ed25519PublicKey.from_public_bytes(b64url_decode(material))
            for kid, material in raw.items()
        }
        self._fetched_at = self._monotonic()
        if key_id not in self._keys:
            raise InvalidPermitError(
                "unknown_signing_key", details={"key_id": key_id}
            )
        return self._keys[key_id]

    async def verify(
        self,
        permit: AgentControlPermit,
        request: object,
        operation: str,
        now: datetime,
    ) -> VerifiedAdmission:
        key = await self._verification_key(permit.key_id)
        try:
            key.verify(b64url_decode(permit.signature), canonical_permit_bytes(permit))
        except (InvalidSignature, ValueError) as error:
            raise InvalidPermitError("signature_mismatch") from error

        if operation not in permit.allowed_operations:
            raise InvalidPermitError(
                "operation_not_allowed", details={"operation": operation}
            )
        # authorization_ref 必须绑定到 permit 本体：伪造 ref 不得通过。
        if str(getattr(request, "authorization_ref", "")) != permit.permit_id:
            raise InvalidPermitError(
                "authorization_ref_mismatch",
                details={"expected": "permit_id"},
            )
        if (permit.tenant_id, permit.agent_instance_id) != (
            getattr(request, "tenant_id", None),
            getattr(request, "agent_instance_id", None),
        ):
            raise InvalidPermitError("resource_binding_mismatch")
        # session-bound permit 只能用于同一 session；instance 级
        # （session_id=None）请求不允许用 session permit 放大作用域。
        request_session = getattr(request, "session_id", None)
        if permit.session_id is not None and request_session != permit.session_id:
            raise InvalidPermitError(
                "resource_binding_mismatch", details={"field": "session_id"}
            )
        issued_at = parse_rfc3339(permit.issued_at)
        if issued_at > now:
            raise InvalidPermitError("permit_not_yet_valid")
        if parse_rfc3339(permit.expires_at) <= now:
            raise PermitExpiredError("permit_expired")
        if (
            parse_rfc3339(permit.expires_at) - issued_at
        ).total_seconds() > PERMIT_MAX_TTL_SECONDS:
            raise InvalidPermitError(
                "permit_ttl_exceeds_maximum",
                details={"max_ttl_seconds": PERMIT_MAX_TTL_SECONDS},
            )

        if operation in MUTATION_OPERATIONS:
            if not await self._nonce_store.register(
                permit.nonce,
                str(getattr(request, "command_id", "")),
                str(getattr(request, "idempotency_key", "")),
            ):
                raise InvalidPermitError("nonce_reuse")

        return VerifiedAdmission(
            permit_id=permit.permit_id,
            subject_ref=permit.subject_ref,
            claims_digest=permit.claims_digest,
            key_id=permit.key_id,
            operation=operation,
        )


__all__ = [
    "AgentControlPermitVerifier",
    "InMemoryNonceStore",
    "JwksSource",
    "NonceStore",
    "PERMIT_MAX_TTL_SECONDS",
    "PermitExpiredError",
    "VerifiedAdmission",
    "MUTATION_OPERATIONS",
    "READ_OPERATIONS",
    "b64url_decode",
    "b64url_encode",
    "canonical_permit_bytes",
    "normalize_rfc3339_seconds",
    "parse_rfc3339",
    "sign_permit",
]
