"""Trusted Gateway identity boundary for inbound A2A protocol requests."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from ksadk.a2a.context_store import A2AContextIdentity
from ksadk.a2a.ids import require_a2a_resource_id

_CALLER_PRINCIPAL_TYPES = frozenset({"user", "runtime", "service"})
_AUTHN_MODE_BY_CALLER_TYPE = {
    "user": "api_key",
    "runtime": "workload_permit",
    "service": "gateway_service",
}


@dataclass(frozen=True)
class A2AIngressTargetBinding:
    """The Gateway-verified destination to which one Runtime is bound."""

    account_id: str
    tenant_id: str
    agent_id: str
    runtime_id: str
    a2a_agent_id: str


@dataclass(frozen=True)
class A2AIngressIdentity:
    """Caller identity verified by Gateway mTLS and target-bound forwarding."""

    account_id: str
    tenant_id: str
    caller_principal_type: str
    caller_principal_id: str
    target_agent_id: str
    target_runtime_id: str
    target_a2a_agent_id: str
    authn_mode: str
    caller_runtime_id: str | None = None

    def validate(self) -> None:
        values = {
            "account_id": self.account_id,
            "caller_principal_type": self.caller_principal_type,
            "caller_principal_id": self.caller_principal_id,
            "target_agent_id": self.target_agent_id,
            "target_runtime_id": self.target_runtime_id,
            "target_a2a_agent_id": self.target_a2a_agent_id,
            "authn_mode": self.authn_mode,
        }
        missing = [name for name, value in values.items() if not str(value).strip()]
        if missing:
            raise PermissionError(
                "verified Gateway identity is missing " + ", ".join(missing)
            )
        if self.caller_principal_type not in _CALLER_PRINCIPAL_TYPES:
            raise PermissionError("verified Gateway identity has an unsupported caller type")
        if self.authn_mode != _AUTHN_MODE_BY_CALLER_TYPE[self.caller_principal_type]:
            raise PermissionError("verified Gateway identity has an invalid authentication mode")
        if self.caller_principal_type == "runtime" and not str(
            self.caller_runtime_id or ""
        ).strip():
            raise PermissionError("verified runtime caller identity is missing caller runtime")
        if self.caller_principal_type != "runtime" and self.caller_runtime_id is not None:
            raise PermissionError("verified non-runtime caller identity includes a caller runtime")
        for field_name, limit in (
            ("account_id", 64),
            ("tenant_id", 64),
            ("caller_principal_type", 16),
            ("caller_principal_id", 128),
            ("target_agent_id", 64),
            ("target_runtime_id", 64),
            ("target_a2a_agent_id", 64),
            ("authn_mode", 32),
            ("caller_runtime_id", 64),
        ):
            value = getattr(self, field_name)
            if value is not None and len(str(value)) > limit:
                raise PermissionError(
                    f"verified Gateway identity field {field_name} exceeds {limit} characters"
                )
        if not self.target_agent_id.startswith("ar-"):
            raise PermissionError("verified Gateway identity has an invalid target Agent")
        try:
            require_a2a_resource_id(
                self.target_a2a_agent_id,
                "a2a-agent-",
                field_name="verified Gateway identity target A2A Agent",
            )
        except ValueError as exc:
            raise PermissionError(
                "verified Gateway identity has an invalid target A2A Agent"
            ) from exc

    def context_identity(self) -> A2AContextIdentity:
        return A2AContextIdentity(
            account_id=self.account_id,
            tenant_id=self.tenant_id,
            caller_principal_type=self.caller_principal_type,
            caller_principal_id=self.caller_principal_id,
        )

    def owner_key(self) -> str:
        return "/".join(
            (
                self.account_id.strip(),
                self.tenant_id.strip(),
                self.caller_principal_type.strip(),
                self.caller_principal_id.strip(),
            )
        )


class GatewayIdentityVerifier(ABC):
    """Platform adapter that verifies Gateway mTLS and target-bound forwarding."""

    @abstractmethod
    async def verify(self, request: Request) -> A2AIngressIdentity:
        raise NotImplementedError


class GatewayProbeVerifier(ABC):
    """Verifies a trusted Gateway/server probe for the Runtime-local AgentCard."""

    @abstractmethod
    async def verify_probe(self, request: Request) -> None:
        raise NotImplementedError


class CallableGatewayIdentityVerifier(GatewayIdentityVerifier):
    """Small adapter for product injectors and tests."""

    def __init__(
        self,
        verify: Callable[[Request], Awaitable[A2AIngressIdentity]],
    ) -> None:
        self._verify = verify

    async def verify(self, request: Request) -> A2AIngressIdentity:
        return await self._verify(request)


class CallableGatewayProbeVerifier(GatewayProbeVerifier):
    """Small probe-verifier adapter for product injectors and tests."""

    def __init__(self, verify_probe: Callable[[Request], Awaitable[None]]) -> None:
        self._verify_probe = verify_probe

    async def verify_probe(self, request: Request) -> None:
        await self._verify_probe(request)


class A2ATrustedIdentityResolver:
    """Reads only identity written by verified ingress middleware."""

    state_key = "a2a_identity"

    def __init__(
        self,
        *,
        expected_account_id: str | None = None,
        expected_tenant_id: str | None = None,
        expected_target_agent_id: str | None = None,
        expected_target_runtime_id: str | None = None,
        expected_target_a2a_agent_id: str | None = None,
        target_binding: A2AIngressTargetBinding | None = None,
    ) -> None:
        if target_binding is not None:
            if any(
                value is not None
                for value in (
                    expected_account_id,
                    expected_tenant_id,
                    expected_target_agent_id,
                    expected_target_runtime_id,
                    expected_target_a2a_agent_id,
                )
            ):
                raise ValueError(
                    "pass target_binding or individual expected target fields, not both"
                )
            expected_account_id = target_binding.account_id
            expected_tenant_id = target_binding.tenant_id
            expected_target_agent_id = target_binding.agent_id
            expected_target_runtime_id = target_binding.runtime_id
            expected_target_a2a_agent_id = target_binding.a2a_agent_id
        self._expected_account_id = str(expected_account_id or "").strip() or None
        self._expected_tenant_id = expected_tenant_id
        self._expected_target_agent_id = str(expected_target_agent_id or "").strip() or None
        self._expected_target_runtime_id = str(expected_target_runtime_id or "").strip() or None
        self._expected_target_a2a_agent_id = str(expected_target_a2a_agent_id or "").strip() or None

    def resolve(self, request: Request) -> A2AIngressIdentity:
        raw_state = request.scope.get("state")
        state = raw_state if isinstance(raw_state, dict) else {}
        identity = state.get(self.state_key)
        if not isinstance(identity, A2AIngressIdentity):
            raise PermissionError("verified Gateway identity is required for inbound A2A")
        self._validate(identity)
        return identity

    def _validate(self, identity: A2AIngressIdentity) -> None:
        identity.validate()
        identity.context_identity().canonical_owner()
        if (
            self._expected_account_id is not None
            and identity.account_id != self._expected_account_id
        ):
            raise PermissionError("verified Gateway identity targets a different account")
        if (
            self._expected_tenant_id is not None
            and identity.tenant_id != self._expected_tenant_id
        ):
            raise PermissionError("verified Gateway identity targets a different tenant")
        if (
            self._expected_target_agent_id is not None
            and identity.target_agent_id != self._expected_target_agent_id
        ):
            raise PermissionError("verified Gateway identity targets a different Agent")
        if (
            self._expected_target_runtime_id is not None
            and identity.target_runtime_id != self._expected_target_runtime_id
        ):
            raise PermissionError("verified Gateway identity targets a different Runtime")
        if (
            self._expected_target_a2a_agent_id is not None
            and identity.target_a2a_agent_id != self._expected_target_a2a_agent_id
        ):
            raise PermissionError("verified Gateway identity targets a different A2A Agent")


class A2AGatewayIdentityMiddleware(BaseHTTPMiddleware):
    """Verify Gateway identity before JSON-RPC or HTTP+JSON A2A handlers run."""

    def __init__(
        self,
        app: Any,
        *,
        verifier: GatewayIdentityVerifier,
        probe_verifier: GatewayProbeVerifier | None = None,
        expected_account_id: str | None = None,
        expected_tenant_id: str | None = None,
        expected_target_agent_id: str | None = None,
        expected_target_runtime_id: str | None = None,
        expected_target_a2a_agent_id: str | None = None,
        target_binding: A2AIngressTargetBinding | None = None,
    ) -> None:
        super().__init__(app)
        self._verifier = verifier
        self._probe_verifier = probe_verifier
        self._identity_resolver = A2ATrustedIdentityResolver(
            expected_account_id=expected_account_id,
            expected_tenant_id=expected_tenant_id,
            expected_target_agent_id=expected_target_agent_id,
            expected_target_runtime_id=expected_target_runtime_id,
            expected_target_a2a_agent_id=expected_target_a2a_agent_id,
            target_binding=target_binding,
        )

    async def dispatch(self, request: Request, call_next: Any):
        if request.url.path == "/.well-known/agent-card.json":
            try:
                if self._probe_verifier is None:
                    raise PermissionError("A2A AgentCard probe verifier is not configured")
                await self._probe_verifier.verify_probe(request)
            except Exception:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "trusted Gateway identity is required for A2A AgentCard"},
                )
        elif request.url.path == "/a2a/jsonrpc" or request.url.path.startswith("/a2a/v1/"):
            try:
                identity = await self._verifier.verify(request)
                self._identity_resolver._validate(identity)
            except Exception:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "verified Gateway identity is required for inbound A2A"},
                )
            raw_state = request.scope.get("state")
            state = raw_state if isinstance(raw_state, dict) else {}
            state[A2ATrustedIdentityResolver.state_key] = identity
            request.scope["state"] = state
        return await call_next(request)


__all__ = [
    "A2AGatewayIdentityMiddleware",
    "A2AIngressTargetBinding",
    "A2AIngressIdentity",
    "A2ATrustedIdentityResolver",
    "CallableGatewayIdentityVerifier",
    "CallableGatewayProbeVerifier",
    "GatewayIdentityVerifier",
    "GatewayProbeVerifier",
]
