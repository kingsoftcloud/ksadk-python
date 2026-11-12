"""Fail-closed managed A2A composition from projected platform locators.

This module is intentionally separate from local A2A helpers: a Runtime with
``KSADK_A2A_RUNTIME_ID`` is a managed product deployment and therefore may not
silently replace durable dependencies with local SQLite or an unverified
identity header.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import ssl
import stat
import time
from pathlib import Path
from typing import Any

import certifi
import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.requests import Request

from ksadk.a2a.bootstrap import (
    MANAGED_MODEL_READ_TIMEOUT_SECONDS,
    AgentEngineA2ABootstrap,
    RuntimeA2AMetadata,
)
from ksadk.a2a.context_store import PostgresA2AContextStore
from ksadk.a2a.control_plane import InternalA2AControlPlaneClient
from ksadk.a2a.identity import (
    A2AIngressIdentity,
    A2AIngressTaskBinding,
    GatewayIdentityVerifier,
    GatewayProbeVerifier,
)
from ksadk.a2a.invocation_checkpoint import PostgresA2AInvocationCheckpointStore
from ksadk.a2a.resume_store import PostgresA2AResumeStateStore
from ksadk.a2a.task_event_outbox_owned import OwnedPostgresA2ATaskEventOutbox
from ksadk.a2a.task_store import build_a2a_task_store
from ksadk.a2a.tool_activity import PublicToolActivityPolicy

_FORWARD_HEADER = "x-agentengine-a2a-assertion"
_ROUTE_TARGET_HEADER = "x-agentengine-route-target"
_FORWARD_ISSUER = "agentengine-a2a-gateway"
_FORWARD_AUDIENCE = "a2a-runtime"


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"managed A2A requires {name}")
    return value


def _managed_database_dsn() -> str:
    """Return the shared Kernel-store DSN using SQLAlchemy's async driver.

    Hosted deployments project ``KSADK_A2A_DATABASE_DSN`` and
    ``AGENT_KERNEL_STORE_DSN`` from the same Kubernetes SecretKeyRef.  The
    Kernel accepts the normal PostgreSQL scheme, while the managed A2A stores
    use SQLAlchemy's async engine and therefore require asyncpg.
    """

    value = _required_env("KSADK_A2A_DATABASE_DSN")
    if value.startswith("postgresql+asyncpg://"):
        return value
    if value.startswith("postgresql://"):
        return "postgresql+asyncpg://" + value[len("postgresql://") :]
    if value.startswith("postgres://"):
        return "postgresql+asyncpg://" + value[len("postgres://") :]
    raise RuntimeError("managed A2A KSADK_A2A_DATABASE_DSN must be a PostgreSQL DSN")


def _projected_file(name: str, *, maximum_bytes: int = 64 * 1024) -> str:
    path = Path(_required_env(name))
    # Kubernetes AtomicWriter exposes each Secret key via ..data/<key>.
    # Resolve a single mounted generation, but never follow a key outside its
    # configured volume. Open every resolved component relative to the volume
    # descriptor with O_NOFOLLOW so a concurrent link swap cannot escape it.
    try:
        volume = path.parent.resolve(strict=True)
        relative = path.resolve(strict=True).relative_to(volume)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        directory = os.open(volume, flags | os.O_DIRECTORY)
        try:
            for component in relative.parts[:-1]:
                child = os.open(component, flags | os.O_DIRECTORY, dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(relative.name, flags | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
    except (OSError, ValueError, RuntimeError) as exc:
        raise RuntimeError(f"managed A2A {name} must name a regular projected file") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum_bytes:
            raise RuntimeError(f"managed A2A {name} is not a safe projected file")
        payload = os.read(descriptor, maximum_bytes + 1).decode("utf-8").strip()
    finally:
        os.close(descriptor)
    if not payload:
        raise RuntimeError(f"managed A2A {name} is empty")
    return payload


def _managed_http_verify() -> ssl.SSLContext | bool:
    """Load the platform CA bundle explicitly while keeping proxy env disabled."""
    ca_env = "KSADK_A2A_CONTROL_PLANE_CA_FILE"
    if not os.getenv(ca_env, "").strip():
        # Compatibility for manually prepared R85 canaries. New managed
        # deployments use the dedicated locator so unrelated HTTPS clients do
        # not inherit a private control-plane trust bundle.
        ca_env = "SSL_CERT_FILE"
    if not os.getenv(ca_env, "").strip():
        return True
    bundle = _projected_file(ca_env, maximum_bytes=512 * 1024)
    try:
        return ssl.create_default_context(cadata=bundle)
    except ssl.SSLError as exc:
        raise RuntimeError("managed A2A control-plane CA file is not a valid CA bundle") from exc



def _gateway_http_verify() -> ssl.SSLContext | bool:
    """Add an explicitly selected Gateway CA without changing control trust."""
    name = "KSADK_A2A_GATEWAY_CA_FILE"
    if not os.getenv(name, "").strip():
        return True
    bundle = _projected_file(name, maximum_bytes=512 * 1024)
    context = ssl.create_default_context(cafile=certifi.where())
    try:
        context.load_verify_locations(cadata=bundle)
    except ssl.SSLError as exc:
        raise RuntimeError("managed A2A Gateway CA file is not a valid CA bundle") from exc
    return context


def _base64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class PostgresA2ACheckpointStore:
    """Readiness gate for the Runtime's platform checkpoint database."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def initialize(self) -> None:
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))


class JWTGatewayIdentityVerifier(GatewayIdentityVerifier, GatewayProbeVerifier):
    """Verifies the Gateway's RS256 forward assertion and binds it to a request."""

    def __init__(self, public_key_pem: str) -> None:
        try:
            self._key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
        except ValueError as exc:
            raise ValueError("invalid managed A2A Gateway public key") from exc

    async def verify(self, request: Request) -> A2AIngressIdentity:
        claims = await self._claims(request)
        task_fields = {"platform_task_id", "target_version_id", "operation"}
        binding = None
        if task_fields.intersection(claims):
            binding = A2AIngressTaskBinding(
                platform_task_id=_claim(claims, "platform_task_id"),
                target_version_id=_claim(claims, "target_version_id"),
                operation=_claim(claims, "operation"),
            )
            binding.validate()
        schema = claims.get("schema")
        product_key = schema == "a2a-forward-identity/v2"
        if product_key:
            if (
                claims.get("caller_principal_type") != "user"
                or claims.get("authn_mode") != "platform_api_key"
                or {"caller_agent_id", "caller_runtime_id", "permit_jti"}.intersection(claims)
                or claims.get("sub") != _claim(claims, "caller_principal_id")
                or binding is None
            ):
                raise PermissionError("Gateway product Key identity is invalid")
            _claim(claims, "key_id")
        elif schema not in (None, "a2a-forward-identity/v1"):
            raise PermissionError("Gateway forward identity schema is unsupported")
        elif claims.get("caller_principal_type", "runtime") != "runtime" or claims.get(
            "authn_mode", "workload_permit"
        ) != "workload_permit":
            raise PermissionError("Gateway workload identity is invalid")
        identity = A2AIngressIdentity(
            account_id=_claim(claims, "account_id"),
            tenant_id=_claim(claims, "tenant_id"),
            caller_principal_type="user" if product_key else "runtime",
            caller_principal_id=_claim(claims, "caller_principal_id"),
            caller_runtime_id=None if product_key else _claim(claims, "caller_runtime_id"),
            sub_account_id=_claim(claims, "sub_account_id") if product_key else None,
            target_agent_id=_claim(claims, "target_agent_id"),
            target_runtime_id=_claim(claims, "target_runtime_id"),
            target_a2a_agent_id=_claim(claims, "target_a2a_agent_id"),
            authn_mode="api_key" if product_key else "workload_permit",
            task_binding=binding,
            verified_request_body=await request.body(),
        )
        if product_key:
            identity.validate()
        return identity

    async def verify_probe(self, request: Request) -> None:
        await self._claims(request)

    async def _claims(self, request: Request) -> dict[str, Any]:
        token = request.headers.get(_FORWARD_HEADER, "").strip()
        if not token:
            raise PermissionError("Gateway forward assertion is required")
        parts = token.split(".")
        if len(parts) != 3:
            raise PermissionError("Gateway forward assertion is malformed")
        try:
            header = json.loads(_base64url(parts[0]))
            claims = json.loads(_base64url(parts[1]))
            signature = _base64url(parts[2])
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise PermissionError("Gateway forward assertion is malformed") from exc
        if not isinstance(header, dict) or header.get("alg") != "RS256":
            raise PermissionError("Gateway forward assertion has an unsupported algorithm")
        if not isinstance(claims, dict):
            raise PermissionError("Gateway forward assertion claims are invalid")
        try:
            self._key.verify(
                signature,
                f"{parts[0]}.{parts[1]}".encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except InvalidSignature as exc:
            raise PermissionError("Gateway forward assertion signature is invalid") from exc
        if claims.get("iss") != _FORWARD_ISSUER or claims.get("aud") != _FORWARD_AUDIENCE:
            raise PermissionError("Gateway forward assertion has an invalid issuer or audience")
        now = time.time()
        if (
            type(claims.get("exp")) not in (int, float)
            or not math.isfinite(claims["exp"])
            or claims["exp"] <= now
        ):
            raise PermissionError("Gateway forward assertion is expired")
        if (
            type(claims.get("iat")) not in (int, float)
            or not math.isfinite(claims["iat"])
            or claims["iat"] > now + 5
            or claims["iat"] >= claims["exp"]
        ):
            raise PermissionError("Gateway forward assertion has an invalid issued time")
        if not _claim(claims, "jti"):
            raise PermissionError("Gateway forward assertion is missing JTI")
        body = await request.body()
        expected_body = hashlib.sha256(body).hexdigest()
        expected_query = hashlib.sha256(request.url.query.encode("utf-8")).hexdigest()
        if (
            claims.get("body_sha256") != expected_body
            or claims.get("query_sha256") != expected_query
        ):
            raise PermissionError("Gateway forward assertion does not bind request content")
        if claims.get("http_method") != request.method or claims.get("path") != request.url.path:
            raise PermissionError("Gateway forward assertion does not bind request route")
        if request.headers.get(_ROUTE_TARGET_HEADER, "").strip() != _claim(
            claims, "target_agent_id"
        ):
            raise PermissionError("Gateway forward assertion does not bind route target")
        return claims


def _claim(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise PermissionError(f"Gateway forward assertion has invalid {name}")
    return value


def build_managed_a2a_bootstrap_from_env() -> AgentEngineA2ABootstrap:
    """Create the only supported managed A2A composition root.

    Environment variables carry IDs and platform projections.  The DSN comes
    from a Kubernetes SecretKeyRef, while Gateway key material is read from a
    projected file, so generated source never embeds credentials or permits.
    """

    inbound_mode = os.getenv("KSADK_A2A_INBOUND_ENABLED", "true").strip().lower()
    if inbound_mode not in {"true", "false"}:
        raise RuntimeError("KSADK_A2A_INBOUND_ENABLED must be true or false")
    inbound_enabled = inbound_mode == "true"

    def inbound_value(name: str) -> str:
        return _required_env(name) if inbound_enabled else os.getenv(name, "").strip()

    database_dsn = _managed_database_dsn()
    gateway_key = _projected_file("KSADK_A2A_GATEWAY_PUBLIC_KEY_FILE")
    public_tool_policy = (
        PublicToolActivityPolicy.from_json(_projected_file("KSADK_A2A_PUBLIC_TOOL_POLICY_FILE"))
        if os.getenv("KSADK_A2A_PUBLIC_TOOL_POLICY_FILE")
        else PublicToolActivityPolicy()
    )
    engine = create_async_engine(
        database_dsn,
        pool_pre_ping=True,
        # Session advisory locks outlive transactions. Detect a disappeared
        # worker's TCP peer promptly instead of inheriting
        # PostgreSQL's multi-hour defaults. These are connection-local and do
        # not expire an idle but reachable owner or change global DB settings.
        connect_args={"server_settings": {
            "tcp_keepalives_idle": "30",
            "tcp_keepalives_interval": "10",
            "tcp_keepalives_count": "3",
        }},
    )
    metadata = RuntimeA2AMetadata(
        account_id=_required_env("KSADK_A2A_ACCOUNT_ID"),
        tenant_id=_required_env("KSADK_A2A_TENANT_ID"),
        agent_id=_required_env("KSADK_A2A_AGENT_ID"),
        a2a_agent_id=inbound_value("KSADK_A2A_AGENT_REGISTRATION_ID"),
        runtime_id=_required_env("KSADK_A2A_RUNTIME_ID"),
        internal_base_url=inbound_value("KSADK_A2A_INTERNAL_BASE_URL"),
        name=inbound_value("KSADK_A2A_AGENT_NAME"),
        version=inbound_value("KSADK_A2A_AGENT_VERSION"),
        skills=(),
    )
    verifier = JWTGatewayIdentityVerifier(gateway_key)
    from ksadk.a2a.timeouts import seconds

    control_seconds = seconds("KSADK_A2A_CONTROL_SECONDS", 25.0)
    control_http_client = httpx.AsyncClient(
        verify=_managed_http_verify(), trust_env=False, timeout=control_seconds
    )
    control_plane = InternalA2AControlPlaneClient(
        _required_env("KSADK_A2A_CONTROL_PLANE_URL"), httpx_client=control_http_client
    )
    # The public Gateway has its own server certificate. A private CA for the
    # control plane must not replace the Gateway's default trust roots.
    hosted_http_client = httpx.AsyncClient(
        verify=_gateway_http_verify(), trust_env=False,
        # Model execution can legitimately outlast the control RPC budget.
        # Managed invocations enforce their own first-event/idle/overall
        # deadlines; keep a finite transport ceiling for direct Space clients.
        timeout=httpx.Timeout(
            seconds("KSADK_A2A_CONNECT_SECONDS", 30.0),
            read=seconds("KSADK_A2A_TRANSPORT_READ_SECONDS", MANAGED_MODEL_READ_TIMEOUT_SECONDS),
        )
    )
    return AgentEngineA2ABootstrap.from_platform(
        runtime_metadata=metadata,
        public_tool_policy=public_tool_policy,
        task_store=build_a2a_task_store(engine=engine, create_table=True),
        context_store=PostgresA2AContextStore(engine),
        checkpoint_store=PostgresA2ACheckpointStore(engine),
        resume_state_store=PostgresA2AResumeStateStore(engine),
        gateway_identity_verifier=verifier,
        gateway_probe_verifier=verifier,
        control_plane=control_plane,
        hosted_http_client=hosted_http_client,
        event_outbox=OwnedPostgresA2ATaskEventOutbox(
            engine, account_id=metadata.account_id, tenant_id=metadata.tenant_id,
            runtime_id=metadata.runtime_id,
        ),
        invocation_checkpoint_store=PostgresA2AInvocationCheckpointStore(
            engine,
            account_id=metadata.account_id,
            tenant_id=metadata.tenant_id,
            runtime_id=metadata.runtime_id,
            lease_engine=engine,
        ),
        inbound_enabled=inbound_enabled,
        outbound_enabled=True,
        public_egress_enabled=False,
        managed_resource_closers=(
            control_http_client.aclose,
            hosted_http_client.aclose,
            engine.dispose,
        ),
    )


__all__ = [
    "JWTGatewayIdentityVerifier",
    "PostgresA2ACheckpointStore",
    "build_managed_a2a_bootstrap_from_env",
]
