"""Trusted Gateway identity boundary for inbound A2A protocol requests."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from ksadk.a2a.context_store import A2AContextIdentity


@dataclass(frozen=True)
class A2AIngressIdentity:
    """Caller identity verified by Gateway mTLS and target-bound forwarding."""

    account_id: str
    tenant_id: str
    caller_principal_type: str
    caller_principal_id: str
    target_agent_id: str

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


class CallableGatewayIdentityVerifier(GatewayIdentityVerifier):
    """Small adapter for product injectors and tests."""

    def __init__(
        self,
        verify: Callable[[Request], Awaitable[A2AIngressIdentity]],
    ) -> None:
        self._verify = verify

    async def verify(self, request: Request) -> A2AIngressIdentity:
        return await self._verify(request)


class A2ATrustedIdentityResolver:
    """Reads only identity written by verified ingress middleware."""

    state_key = "a2a_identity"

    def resolve(self, request: Request) -> A2AIngressIdentity:
        raw_state = request.scope.get("state")
        state = raw_state if isinstance(raw_state, dict) else {}
        identity = state.get(self.state_key)
        if not isinstance(identity, A2AIngressIdentity):
            raise PermissionError("verified Gateway identity is required for inbound A2A")
        identity.context_identity().canonical_owner()
        if not identity.target_agent_id.strip():
            raise PermissionError("verified Gateway identity is missing target agent")
        return identity


class A2AGatewayIdentityMiddleware(BaseHTTPMiddleware):
    """Verify Gateway identity before JSON-RPC or HTTP+JSON A2A handlers run."""

    def __init__(
        self,
        app: Any,
        *,
        verifier: GatewayIdentityVerifier,
        expected_target_agent_id: str | None = None,
    ) -> None:
        super().__init__(app)
        self._verifier = verifier
        self._expected_target_agent_id = str(expected_target_agent_id or "").strip() or None

    async def dispatch(self, request: Request, call_next: Any):
        if request.url.path == "/a2a/jsonrpc" or request.url.path.startswith("/a2a/v1/"):
            try:
                identity = await self._verifier.verify(request)
                identity.context_identity().canonical_owner()
                if not identity.target_agent_id.strip():
                    raise PermissionError("verified Gateway identity is missing target agent")
                if (
                    self._expected_target_agent_id is not None
                    and identity.target_agent_id != self._expected_target_agent_id
                ):
                    raise PermissionError("verified Gateway identity targets a different Agent")
            except (PermissionError, ValueError):
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
    "A2AIngressIdentity",
    "A2ATrustedIdentityResolver",
    "CallableGatewayIdentityVerifier",
    "GatewayIdentityVerifier",
]
