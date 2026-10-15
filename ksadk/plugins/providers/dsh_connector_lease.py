"""Scoped DSH connector token operations, separate from process supervision.

The public lease owns its fields and runtime TTL policy; this mixin signs and
validates the same wire tokens without taking ownership of a running host.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from collections.abc import Mapping
from urllib.parse import urlsplit

from ksadk.plugins.host import PluginHostError

_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]+$")


class DshConnectorLeaseTokens:
    endpoint: str
    profile_digest: str
    _bearer_token: str
    _browser_token: str
    web_route_count: int

    def _runtime_token_lifetime(self) -> int:
        raise NotImplementedError

    def headers(self) -> dict[str, str]:
        """Return fresh request headers without storing them in descriptors/logs."""

        return {"Authorization": f"Bearer {self._bearer_token}"}

    def companion_bearer_token(
        self, *, plugin_id: str, generation_id: str, handle: str,
        tool_name: str, call_id: str, ttl_seconds: int = 60,
    ) -> str:
        """Sign only an opaque Python grant and one exact native tool call."""
        if (not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", plugin_id)
            or not re.fullmatch(r"dshgen_[A-Za-z0-9_-]{24,96}", generation_id)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", handle)
            or not _TOOL_NAME.fullmatch(tool_name)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", call_id)
            or not 1 <= ttl_seconds <= 120):
            raise ValueError("invalid companion invocation scope")
        payload = {
            "v": 3, "profileDigest": self.profile_digest,
            "pluginId": plugin_id, "generationId": generation_id,
            "handle": handle, "callId": call_id,
            "aliases": {tool_name: tool_name},
            "exp": int(time.time()) + ttl_seconds,
            "jti": secrets.token_urlsafe(24),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        message = f"ks3.{encoded}"
        signature = base64.urlsafe_b64encode(
            hmac.new(self._bearer_token.encode(), message.encode(), hashlib.sha256).digest()
        ).rstrip(b"=").decode()
        return f"{message}.{signature}"

    def resource_bearer_token(self, tool_aliases: Mapping[str, str], resource_leases) -> str:
        """Mint scope v2 from host-issued, activation-bound resource leases."""
        from ksadk.resource_runtime.leases import ResourceLease

        leases = tuple(resource_leases)
        if not leases or any(not isinstance(lease, ResourceLease) for lease in leases):
            raise ValueError("Resource scope requires host-issued leases")
        first = leases[0].scope
        if first.profile_digest != self.profile_digest:
            raise ValueError("Resource scope belongs to another profile")
        aliases = dict(tool_aliases)
        if not 1 <= len(aliases) <= 32 or any(
            not isinstance(alias, str)
            or not isinstance(source, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", alias)
            or not _TOOL_NAME.fullmatch(source)
            for alias, source in aliases.items()
        ):
            raise ValueError("Resource scope requires explicit valid tool aliases")
        resources = {}
        lifetime = 600
        for lease in leases:
            scope = lease.scope
            if (
                scope.generation_id != first.generation_id
                or scope.activation_id != first.activation_id
                or scope.build_digest != first.build_digest
                or scope.binding_snapshot_digest != first.binding_snapshot_digest
                or scope.identity != first.identity
                or scope.profile_digest != first.profile_digest
            ):
                raise ValueError("Resource scope cannot mix activations")
            lifetime = min(lifetime, math.floor(lease.expires_at - time.monotonic()))
            for operation in scope.allowed_operations:
                if operation not in aliases.values():
                    continue
                if operation in resources:
                    raise ValueError("Resource operation has ambiguous bindings")
                resources[operation] = {
                    "handle": lease.handle,
                    "bindingId": scope.binding_id,
                    "operation": operation,
                }
        if lifetime < 1 or not resources:
            raise ValueError("Resource scope has no live operations")
        issued = int(time.time())
        payload = {
            "v": 2,
            "profileDigest": self.profile_digest,
            "generationId": first.generation_id,
            "activationId": first.activation_id,
            "buildDigest": first.build_digest,
            "bindingSnapshotDigest": first.binding_snapshot_digest,
            "identity": first.identity.model_dump(by_alias=True, mode="json"),
            "aliases": aliases,
            "resources": resources,
            "issuedAt": issued,
            "expiresAt": issued + lifetime,
            "jti": secrets.token_urlsafe(24),
        }
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        message = f"ks2.{encoded}"
        signature = (
            base64.urlsafe_b64encode(
                hmac.new(self._bearer_token.encode(), message.encode(), hashlib.sha256).digest()
            )
            .rstrip(b"=")
            .decode()
        )
        token = f"{message}.{signature}"
        if len(token) > 12 * 1024:
            raise ValueError("Resource scope exceeds header size limit")
        return token

    def runtime_token_valid(self, token: str) -> bool:
        """Check our own scoped token's signature/expiry without exposing it.

        This local check cannot prove server-side revocation or liveness. The
        owner must also check its issued-token ledger and current generation.
        """
        try:
            if not isinstance(token, str) or len(token) > 12 * 1024:
                return False
            prefix, encoded, signature = token.split(".")
            if prefix != "ks1":
                return False
            expected = hmac.new(
                self._bearer_token.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
            ).digest()
            actual = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
            if not hmac.compare_digest(expected, actual):
                return False
            payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            return (
                payload.get("v") == 1
                and payload.get("profileDigest") == self.profile_digest
                and type(payload.get("exp")) is int
                and payload["exp"] > time.time()
            )
        except (ValueError, TypeError, UnicodeError, AttributeError):
            return False

    def browser_url(self) -> str:
        """Return the one-time full Core DSH browser handoff URL."""

        if self.web_route_count < 1 or not self._browser_token:
            raise PluginHostError(
                "dsh_core_web_unavailable",
                "The selected DSH Profile does not expose the Core Web application",
            )
        parsed = urlsplit(self.endpoint)
        return f"http://127.0.0.1:{parsed.port}/?token={self._browser_token}"

    def bearer_token_for_runtime(self, tool_aliases: Mapping[str, str]) -> str:
        """Mint a generation-bound token limited to explicit tool names."""

        aliases = {
            str(alias).strip(): str(source).strip() for alias, source in tool_aliases.items()
        }
        if (
            not aliases
            or len(aliases) != len(tool_aliases)
            or len(aliases) > 32
            or any(
                not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", alias) or not _TOOL_NAME.fullmatch(source)
                for alias, source in aliases.items()
            )
        ):
            raise PluginHostError(
                "dsh_capability_scope_invalid",
                "DSH capability tokens require an explicit valid tool scope",
            )
        payload = json.dumps(
            {
                "v": 1,
                "profileDigest": self.profile_digest,
                "exp": int(time.time()) + self._runtime_token_lifetime(),
                "jti": secrets.token_urlsafe(16),
                "aliases": dict(sorted(aliases.items())),
            },
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = hmac.new(self._bearer_token.encode("utf-8"), encoded, hashlib.sha256).digest()
        encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=")
        token = f"ks1.{encoded.decode('ascii')}.{encoded_signature.decode('ascii')}"
        if len(token) > 12 * 1024:
            raise PluginHostError(
                "dsh_capability_scope_too_large",
                "DSH capability token scope exceeds the HTTP header budget",
            )
        return token
