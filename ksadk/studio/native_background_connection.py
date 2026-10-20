"""Host-owned typed background connection target for E2B sandbox lifecycle.

The background sandbox connection identity is a host-declared
``BackgroundConnectionTarget`` that mirrors the SDK ``ConnectionTarget`` —
the five identity fields ``connection_ref`` / ``tenant_ref`` /
``principal_ref`` / ``endpoint`` / ``auth_mode`` — plus the E2B transport
fields ``domain`` and ``traffic_source``. The API key is never persisted;
it is resolved by the host credential resolver at RPC time under the same
authority as launch.

Authority is the *full typed identity*, never just the endpoint. Two hosts
that share an endpoint but differ in ``tenant_ref`` / ``principal_ref`` are
different authorities and must not read, stop or terminate each other's
sandbox. No host declaration means the E2B background capability is
unavailable: the outcome is ``unknown`` with zero backend calls, never a
silent fallback to the environment.

This mirrors two existing host-trust patterns:

* ``ksadk.resource_runtime.skill_runtime.create_bound_skill_runtime``
  validates ``target.connection == current_connection`` before constructing
  an ``ExplicitE2BConnection`` — the host revalidates account/grant first;
  only secret values may rotate.
* ``ksadk.studio.resource_connections.ResourceConnectionRepository`` holds
  declared ``ConnectionTarget`` records with credential references resolved
  by the host after admission — the trusted source for typed connection
  identity and credentials, never ambient env.

The type deliberately lives in the Studio layer; it is not moved into the
Skill control plane and does not invent a ``local-user`` authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator, model_validator

from ksadk.resource_runtime.snapshots import ConnectionTarget
from ksadk.sandbox.base import SandboxError
from ksadk.sandbox.e2b_connection import ExplicitE2BConnection

# The five identity fields serialized into the frozen sandbox.json connection.
# The API key is never part of this projection.
_IDENTITY_FIELDS = ("connection_ref", "tenant_ref", "principal_ref", "endpoint", "auth_mode")


class BackgroundConnectionTarget(BaseModel):
    """Non-sensitive frozen connection identity for an E2B background sandbox.

    ``connection`` is the canonical SDK ``ConnectionTarget`` (the five identity
    fields). ``domain`` and ``traffic_source`` are the E2B transport parameters
    needed to build an ``ExplicitE2BConnection``. No credential is carried here;
    authority is the full typed identity, never just the endpoint.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    connection: ConnectionTarget
    domain: str
    traffic_source: str

    @field_validator("domain")
    @classmethod
    def _domain_is_hostname(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"[a-zA-Z0-9]+(?:[.-][a-zA-Z0-9-]+)*", value):
            raise ValueError("E2B domain must be an explicit hostname")
        return value

    @model_validator(mode="after")
    def _e2b_https_token(self) -> "BackgroundConnectionTarget":
        # The SDK ExplicitE2BConnection requires an HTTPS origin and a single
        # bearer token. The declared identity must match that contract: an
        # HTTPS endpoint with no path and token auth_mode. A signed/STS
        # connection is a different credential shape and is not background-E2B.
        url = urlsplit(self.connection.endpoint)
        if url.scheme != "https" or url.path not in {"", "/"}:
            raise ValueError(
                "Background E2B connection requires an explicit HTTPS origin endpoint"
            )
        if self.connection.auth_mode != "token":
            raise ValueError("Background E2B connection requires token auth_mode")
        return self

    def to_dict(self) -> dict:
        """Serialize the non-sensitive typed identity for sandbox.json freezing.

        Round-trips through :meth:`from_dict`. The API key is never included.
        """
        identity = self.connection.model_dump()
        return {
            **{name: identity[name] for name in _IDENTITY_FIELDS},
            "domain": self.domain,
            "traffic_source": self.traffic_source,
        }

    @classmethod
    def from_dict(cls, data: object) -> "BackgroundConnectionTarget | None":
        """Reconstruct a target frozen in sandbox.json, or None if damaged.

        None means the frozen connection is not a provable typed identity
        (legacy api_url/domain/traffic_source record, or corrupt). Callers
        treat None as ``unknown`` — never as an absent/absent authority.
        """
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                connection=ConnectionTarget(
                    connection_ref=data.get("connection_ref", ""),
                    tenant_ref=data.get("tenant_ref", ""),
                    principal_ref=data.get("principal_ref", ""),
                    endpoint=data.get("endpoint", ""),
                    auth_mode=data.get("auth_mode", "token"),
                ),
                domain=data.get("domain", ""),
                traffic_source=data.get("traffic_source", ""),
            )
        except Exception:
            return None

    def authority_matches(self, other: "BackgroundConnectionTarget") -> bool:
        """Full typed identity equality — the authority test.

        Key rotation under the same identity is permitted (the key is not part
        of the target); a different ``tenant_ref`` / ``principal_ref`` is a
        different authority even when the endpoint is identical.
        """
        return self == other


class BackgroundCredentialResolver(Protocol):
    """Host credential provider: resolve the E2B api_key for a declared target.

    Same authority as launch. The resolver must refuse a target whose identity
    differs from the connection it is bound to — it never hands another
    authority's key to a different declaration. Key rotation under the same
    identity is permitted.
    """

    def resolve_api_key(self, target: BackgroundConnectionTarget) -> SecretStr: ...


@dataclass(frozen=True)
class BackgroundHostDeclaration:
    """Host-declared background connection: a typed target + credential resolver.

    Constructed by the Studio host from a declared resource connection (see
    :func:`build_background_host_declaration`). ``None`` means the E2B
    background capability is not declared — launch/query/stop/cold/delete all
    report ``unknown`` with zero backend calls rather than falling back to env.
    """

    target: BackgroundConnectionTarget
    resolver: BackgroundCredentialResolver


def make_explicit_connection(
    declaration: BackgroundHostDeclaration,
) -> ExplicitE2BConnection:
    """Construct an ``ExplicitE2BConnection`` from a host declaration.

    The endpoint comes from the typed target (``connection.endpoint``); the key
    is resolved by the host credential resolver under the same authority as
    launch. The key is never persisted — only the non-sensitive
    :meth:`BackgroundConnectionTarget.to_dict` projection is frozen.
    """
    target = declaration.target
    return ExplicitE2BConnection(
        api_url=target.connection.endpoint,
        domain=target.domain,
        api_key=declaration.resolver.resolve_api_key(target),
        traffic_source=target.traffic_source,
    )


class _ResourceConnectionCredentialResolver:
    """Credential resolver backed by the host ``ResourceConnectionRepository``.

    The bound ``ConnectionTarget`` is the authority: a declaration whose
    ``connection`` differs is refused before any secret lookup. The repository
    itself re-verifies the target against the declared record on each
    resolution (``RESOURCE_CONNECTION_CHANGED`` on drift), so a rotated record
    revision cannot silently serve another authority's declaration.
    """

    def __init__(self, resource_connections: object, connection_target: ConnectionTarget) -> None:
        self._resource_connections = resource_connections
        self._connection_target = connection_target

    def resolve_api_key(self, target: BackgroundConnectionTarget) -> SecretStr:
        if target.connection != self._connection_target:
            raise SandboxError("background credential resolver authority mismatch")
        credentials = self._resource_connections.resolve_credentials(self._connection_target)  # type: ignore[attr-defined]
        token = getattr(credentials, "token", None)
        if token is None:
            raise SandboxError("background connection has no token credential")
        return token


def build_background_host_declaration(
    resource_connections: object,
    *,
    connection_ref: str,
    domain: str,
    traffic_source: str,
) -> BackgroundHostDeclaration:
    """Build a host declaration from a declared resource connection.

    The connection identity (``connection_ref`` / ``tenant_ref`` /
    ``principal_ref`` / ``endpoint`` / ``auth_mode``) comes from the host's
    declared resource connection record — never ambient env. A declared
    ``connection_ref`` that has no record is a host configuration error and
    surfaces loudly (it is not silently dropped to ``unavailable``).

    The caller decides whether background E2B is declared at all: passing
    ``connection_ref=None`` at the host means no declaration (the capability is
    ``unknown`` with zero backend calls). This factory is only invoked when the
    host has explicitly declared a connection.
    """
    record = resource_connections.get(connection_ref)  # type: ignore[attr-defined]
    target = BackgroundConnectionTarget(
        connection=record.target, domain=domain, traffic_source=traffic_source,
    )
    resolver = _ResourceConnectionCredentialResolver(resource_connections, record.target)
    return BackgroundHostDeclaration(target=target, resolver=resolver)
