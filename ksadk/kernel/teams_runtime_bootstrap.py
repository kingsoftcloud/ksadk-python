"""Opt-in Cloud Runtime Teams composition over the original Kernel worker.

Deployment configuration fixes the Server and immutable Cloud target. Contexts
and short callback permissions come only from that Server after it verifies the
incoming signed execution permit. No shared runtime token, local issuer, custom
run loop, or request-supplied policy is accepted here.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ksadk.kernel.authorization import b64url_decode
from ksadk.kernel.execution_host_ingress import (
    HostBinding,
    KernelTeamsExecutionHost,
    TrustedGrantWindow,
    TrustedPreparation,
)
from ksadk.kernel.teams_execution_context import (
    ContextConflict,
    PostgresTeamsExecutionContextRegistry,
    PreparedTeamsContext,
    StoreIdentityMismatch,
)
from ksadk.plugins.teams.cloud_contract_fingerprints import teams_cloud_contract_digest
from ksadk.plugins.teams.cloud_contracts import CloudTarget, digest
from ksadk.plugins.teams.cloud_permits import (
    BindingProbePermit,
    PermitError,
    TeamsCallbackPermit,
    TeamsExecutionPermit,
    TeamsPermitVerifier,
    _time,
)


@dataclass(frozen=True)
class CloudTeamsHostConfig:
    server_url: str
    issuer: str
    target: CloudTarget
    provider_ref: str
    state_dir: Path

    def __post_init__(self):
        url = urlsplit(self.server_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Teams Runtime requires a fixed HTTPS Server API URL")
        if not self.issuer or not self.provider_ref:
            raise ValueError("Teams issuer and provider reference are required")
        object.__setattr__(self, "target", CloudTarget.model_validate(self.target))
        object.__setattr__(self, "state_dir", Path(self.state_dir))


class _PolicyDispatch:
    def __init__(self, host, previous):
        self.host, self.previous = host, previous

    async def resolve(self, ref, *, request):
        if request.metadata.get("teams_context_ref"):
            return await self.host.resolve(ref, request=request)
        if self.previous is None:
            raise ContextConflict("ordinary execution policy resolver unavailable")
        return await self.previous.resolve(ref, request=request)


class CloudTeamsRuntimeHost:
    """RuntimeAppConfig.teams_host_factory implementation, PG production only.

    Set KSADK_TEAMS_RUNTIME_SERVER_URL, KSADK_TEAMS_RUNTIME_TARGET,
    KSADK_TEAMS_RUNTIME_PROVIDER_REF and
    KSADK_TEAMS_PERMIT_ISSUER variables to enable the environment entry point.
    TARGET is the frozen CloudTarget JSON; SERVER_URL ends at the Teams API base.
    The optional STATE_DIR stores isolated per-attempt workspaces, not authority.
    Contexts/callback permissions/inbox identity are in the original shared PG.
    """

    def __init__(
        self,
        config: CloudTeamsHostConfig,
        *,
        client=None,
        effect_adapters=None,
        loaded_build_factory=None,
    ):
        self.config = config
        self.client = client or httpx.AsyncClient(timeout=10, follow_redirects=False)
        self.owns_client = client is None
        self.runtime = self.registry = None
        self.keys = {}
        self.keys_loaded_at = None
        self.policy_mounted = False
        self.effect_adapters = dict(effect_adapters or {})
        # Explicit trusted Code entrypoint composition; no guessed archive path
        # or user environment field can produce loaded-build evidence.
        self.loaded_build_factory = loaded_build_factory
        self.loaded_build_provider = None
        self.effect_journal = None
        self.prepare_permits = {}
        from ksadk.kernel.teams_material_transport import TeamsHTTPMaterialTransport
        from ksadk.kernel.teams_materials import TeamsMaterializer

        self.material_transport = TeamsHTTPMaterialTransport(
            config.server_url,
            authorization=self._material_authorization,
        )
        self.materializer = TeamsMaterializer(
            config.state_dir / "materials",
            fetch_manifest=self.material_transport.fetch_manifest,
            open_blob=self.material_transport.open_blob,
        )

    @classmethod
    def from_env(cls, *, loaded_build_factory=None):
        names = (
            "KSADK_TEAMS_RUNTIME_SERVER_URL",
            "KSADK_TEAMS_RUNTIME_TARGET",
            "KSADK_TEAMS_RUNTIME_PROVIDER_REF",
            "KSADK_TEAMS_PERMIT_ISSUER",
        )
        values = [os.environ.get(name, "").strip() for name in names]
        # The issuer is also used by Studio Node; it alone does not enable Cloud.
        if not any(values[:3]):
            return None
        if not all(values):
            raise RuntimeError("incomplete trusted Teams Runtime deployment configuration")
        return cls(
            CloudTeamsHostConfig(
                server_url=values[0],
                target=CloudTarget.model_validate_json(values[1]),
                provider_ref=values[2],
                issuer=values[3],
                state_dir=Path(os.environ.get("KSADK_TEAMS_RUNTIME_STATE_DIR", "/tmp/ksadk-teams")),
            ),
            loaded_build_factory=loaded_build_factory,
        )

    def wrap_adapter_provider(self, provider):
        if provider is None:
            raise RuntimeError("Teams Host requires the actual ManagedHarness adapter")

        def create():
            from ksadk.harness.managed_runtime import ManagedHarnessRuntimeAdapter

            adapter = provider()
            if not isinstance(adapter, ManagedHarnessRuntimeAdapter):
                raise RuntimeError("Teams Host requires ManagedHarness policy enforcement")
            current = adapter._execution_policy_resolver
            if not isinstance(current, _PolicyDispatch) or current.host is not self:
                adapter._execution_policy_resolver = _PolicyDispatch(self, current)
            self.policy_mounted = True
            return adapter

        return create

    async def before_start(self, runtime):
        from ksadk.kernel.ingress import set_teams_ingress_gate
        from ksadk.kernel.postgres_store import (
            PostgresAgentKernelStore,
            PostgresFencedSessionEventStore,
        )
        from ksadk.kernel.teams_host_http import TeamsNativeIngressGate
        from ksadk.sessions.postgres_service import PostgresSessionService

        if (
            runtime.config.authority_mode != "hosted"
            or runtime.config.agent_instance_id != self.config.target.agentInstanceId
            or not isinstance(runtime.kernel_store, PostgresAgentKernelStore)
            or not isinstance(runtime.session_events, PostgresFencedSessionEventStore)
            or not isinstance(runtime.config.session_service, PostgresSessionService)
            or not self.policy_mounted
        ):
            raise ContextConflict("Teams Host requires a matching hosted, shared policy runtime")
        self.runtime = runtime
        if self.loaded_build_factory is not None:
            from ksadk.plugins.teams.build_artifacts import LoadedBuildProvider

            self.loaded_build_provider = self.loaded_build_factory(runtime)
            if not isinstance(self.loaded_build_provider, LoadedBuildProvider):
                raise ContextConflict("trusted Code loaded-build factory returned no verifier")
        self.registry = PostgresTeamsExecutionContextRegistry(runtime.kernel_store)
        await self.registry.initialize()
        from ksadk.kernel.teams_tool_runtime import open_effect_journal

        self.effect_journal = await open_effect_journal(self.registry)
        await self._refresh_keys(force=True)
        # Mount before workers can recover any durable Teams command. The
        # canonical native ingress remains the only execution submission path.
        set_teams_ingress_gate(
            TeamsNativeIngressGate(self.resolve_host, self.registry.owns_session)
        )
        runtime.teams_host_descriptor = self.descriptor

    async def _request(self, path, *, body=None, permit=None, callback=False):
        headers = {}
        if permit is not None:
            name = "X-Teams-Callback-Permit" if callback else "X-Teams-Execution-Permit"
            headers[name] = json.dumps(permit.model_dump(mode="json"), ensure_ascii=True)
        try:
            response = await self.client.request(
                "GET" if body is None else "POST",
                self.config.server_url.rstrip("/") + path,
                json=body,
                headers=headers,
            )
            response.raise_for_status()
            if len(response.content) > 2 * 1024 * 1024:
                raise ContextConflict("trusted Teams response exceeds size limit")
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ContextConflict("trusted Teams Server unavailable or rejected request") from exc

    async def _refresh_keys(self, *, force=False):
        if (
            not force
            and self.keys_loaded_at is not None
            and time.monotonic() - self.keys_loaded_at < 5
        ):
            return
        data = await self._request("/jwks")
        keys = {}
        for item in data.get("keys", []):
            if (
                item.get("kty") != "OKP"
                or item.get("crv") != "Ed25519"
                or not isinstance(item.get("kid"), str)
                or not item["kid"]
                or not isinstance(item.get("x"), str)
                or item["kid"] in keys
            ):
                raise ContextConflict("invalid trusted Teams verification key")
            keys[item["kid"]] = Ed25519PublicKey.from_public_bytes(b64url_decode(item["x"]))
        if not keys:
            raise ContextConflict("trusted Teams verification keys unavailable")
        self.keys, self.keys_loaded_at = keys, time.monotonic()

    def verifier(self):
        return TeamsPermitVerifier(self.keys, issuer=self.config.issuer)

    async def _database_clock(self):
        """Use the same time authority as PG grant checks, advance by monotonic.

        Counting the whole query RTT is conservative; node wall-clock rollback
        cannot extend a short callback permission beyond its signed expiry.
        """
        if self.runtime is None:
            raise ContextConflict("Teams Host is not started")
        started = time.monotonic()
        async with self.runtime.kernel_store._connection() as connection:
            observed = await connection.fetchval("SELECT clock_timestamp()")
        return lambda: observed + timedelta(seconds=max(0, time.monotonic() - started))

    def _host(self, authority_id, binding_ref, *, loader=None, renewal_loader=None, clock=None):
        if self.runtime is None or self.registry is None:
            raise ContextConflict("Teams Host has not completed runtime startup")

        async def unavailable(_):
            raise PermitError("trusted context loading requires a signed execution operation")

        return KernelTeamsExecutionHost(
            kernel=self.runtime.kernel,
            store=self.runtime.kernel_store,
            events=self.runtime.session_events,
            registry=self.registry,
            binding=HostBinding(
                authority_id=authority_id,
                binding_ref=binding_ref,
                provider_ref=self.config.provider_ref,
                agent_instance_id=self.config.target.agentInstanceId,
                target=self.config.target.model_dump(mode="json"),
                bundle_digest=self.runtime.config.bundle_digest,
                contract_digest=teams_cloud_contract_digest(),
            ),
            permit_verifier=self.verifier(),
            context_loader=loader or unavailable,
            session_ensurer=self._ensure_session,
            policy_factory=self.policy,
            material_preparer=self.materializer.prepare,
            effect_journal=self.effect_journal,
            effect_adapters=self.effect_adapters,
            loaded_build_provider=self.loaded_build_provider,
            teams_tools_ready=True,
            callback_ready=True,
            renewal_loader=renewal_loader,
            callback_registrar=self._register_callback,
            policy_enforcement_ready=self.policy_mounted,
            canonical_events_durable=True,
            canonical_events_shared=True,
            clock=clock or (lambda: datetime.now(timezone.utc)),
        )

    async def descriptor(self):
        # These labels select no execution. Binding/authority are independently
        # verified from signed probe or persisted context at each operation.
        host = self._host("descriptor", "descriptor")
        capabilities = host.capability_snapshot()
        return {
            "capabilities": capabilities,
            "capabilitiesDigest": digest(capabilities),
            "bundleDigest": host.binding.bundle_digest,
            "contractDigest": host.binding.contract_digest,
            "agentInstanceId": host.binding.agent_instance_id,
            "storeIncarnation": await self.registry.incarnation(),
        }

    async def _ensure_session(self, context):
        sessions = self.runtime.config.session_service
        session = await sessions.get_session_metadata(context.ref.sessionId)
        if session is None:
            session = await sessions.create_session(
                agent_id=context.grant.agent_instance_id,
                user_id=context.owner_subject,
                session_id=context.ref.sessionId,
            )
        return session

    def _deployment_check(self, context):
        if context.command.tenant_id != self.runtime.config.tenant_id:
            raise ContextConflict("prepared context tenant differs from deployed Kernel")
        self._host(context.ref.authorityId, context.ref.bindingRef)._binding_check(context)

    async def _load(self, context_ref, permit, *, operation_id=None):
        clock = await self._database_clock()
        operation = (
            "renew_grant"
            if operation_id
            else ("prepare" if "prepare" in permit.allowedOperations else "ensure_session")
        )
        # Authenticate before asking Server for context. These are signed claims,
        # then the returned persisted context is compared again by the Host.
        self.verifier().verify(
            permit,
            TeamsExecutionPermit,
            operation=operation,
            now=clock(),
            grant_expires_at=_time(permit.expiresAt),
            expected={
                name: getattr(permit, name)
                for name in (
                    "authorityId",
                    "subjectRef",
                    "sessionId",
                    "commandId",
                    "payloadDigest",
                    "policyDigest",
                    "attemptEpoch",
                    "grantRevision",
                    "leaderEpoch",
                    "dispatchEpoch",
                )
            }
            | {"agentInstanceId": self.config.target.agentInstanceId, "permitKind": "execute"},
        )
        started = time.monotonic()
        data = await self._request(
            "/runtime/contexts/resolve",
            permit=permit,
            body={"contextRef": context_ref}
            | ({"operationId": operation_id} if operation_id else {}),
        )
        context = PreparedTeamsContext.model_validate(data["context"])
        if context.context_ref != context_ref:
            raise ContextConflict("trusted Server returned a different context")
        self._deployment_check(context)
        self.prepare_permits[context_ref] = permit
        if data.get("storeIncarnation") != await self.registry.incarnation():
            raise StoreIdentityMismatch("Server references another original Kernel store")
        window = TrustedGrantWindow(
            expires_at=data["expiresAt"],
            server_time=_time(data["serverTime"]),
            request_started_monotonic=started,
            callback_permit=TeamsCallbackPermit.model_validate(data.get("callbackPermit")),
        )
        window.remaining()
        return TrustedPreparation(context, window)

    async def resolve_host(self, *, context_ref, permit):
        await self._refresh_keys(force=permit.kid not in self.keys)
        clock = await self._database_clock()
        if context_ref is None:
            probe = BindingProbePermit.model_validate(permit)
            return self._host(probe.authorityId, probe.bindingRef, clock=clock)
        permit = TeamsExecutionPermit.model_validate(permit)
        if self.registry is None:
            raise ContextConflict("Teams Host is not started")
        preparation = None
        context = await self.registry.get(
            context_ref, expected_incarnation=await self.registry.incarnation()
        )
        if context is None:
            if permit.permitKind == "recovery":
                # An authentic read permission cannot recreate the missing
                # original context on a newly initialized store.
                self.verifier().verify(
                    permit,
                    TeamsExecutionPermit,
                    operation=permit.allowedOperations[0],
                    now=clock(),
                    expected={
                        name: getattr(permit, name)
                        for name in (
                            "authorityId",
                            "subjectRef",
                            "sessionId",
                            "commandId",
                            "payloadDigest",
                            "policyDigest",
                            "attemptEpoch",
                            "grantRevision",
                        )
                    }
                    | {"agentInstanceId": self.config.target.agentInstanceId},
                )
                raise StoreIdentityMismatch("original prepared context unavailable")
            preparation = await self._load(context_ref, permit)
            context = preparation.context
        self._deployment_check(context)

        async def load(ref):
            if ref != context_ref:
                raise ContextConflict("context loader reference changed")
            return preparation or await self._load(ref, permit)

        async def renew(ref, operation_id):
            if ref != context_ref:
                raise ContextConflict("renewal loader reference changed")
            resolved = await self._load(ref, permit, operation_id=operation_id)
            if resolved.context != context:
                raise ContextConflict("renewal changed the original immutable context")
            return resolved.grant_window

        return self._host(
            context.ref.authorityId,
            context.ref.bindingRef,
            loader=load,
            renewal_loader=renew,
            clock=clock,
        )

    async def _verify_callback(self, context, permit, barrier, operation):
        ref = context.ref
        clock = await self._database_clock()
        return self.verifier().verify(
            permit,
            TeamsCallbackPermit,
            operation=operation,
            now=clock(),
            grant_expires_at=_time(barrier.grant.expires_at),
            expected={
                "permitKind": "execute",
                "authorityId": ref.authorityId,
                "groupId": ref.groupId,
                "teamRunId": ref.teamRunId,
                "memberId": ref.memberId,
                "sessionId": ref.sessionId,
                "commandId": ref.commandId,
                "agentInstanceId": context.grant.agent_instance_id,
                "bundleDigest": ref.bundleDigest,
                "policyDigest": context.policy_digest,
                "attemptEpoch": ref.attemptEpoch,
                "leaderEpoch": ref.leaderEpoch,
                "grantRevision": barrier.grant.revision,
            },
        )

    async def _register_callback(self, context, window, barrier):
        permit = await self._verify_callback(context, window.callback_permit, barrier, "policy")
        await self.registry.put_callback(
            context.context_ref,
            permit,
            expected_incarnation=await self.registry.incarnation(),
        )

    async def _callback(self, context, *, operation):
        await self.runtime.kernel.require_execution_grant(context.grant)
        barrier = await self.runtime.kernel.get_execution_grant(context.grant)
        permit = await self.registry.get_callback(
            context.context_ref,
            barrier.grant.revision,
            expected_incarnation=await self.registry.incarnation(),
        )
        if permit is None:
            raise PermitError("current grant revision has no Server callback permission")
        await self._refresh_keys(force=permit.kid not in self.keys)
        return await self._verify_callback(context, permit, barrier, operation)

    async def resolve(self, ref, *, request):
        context = await self.registry.get(
            request.metadata.get("teams_context_ref"),
            expected_incarnation=await self.registry.incarnation(),
        )
        if context is None:
            raise StoreIdentityMismatch("original persisted policy context unavailable")
        self._deployment_check(context)
        return await self._host(context.ref.authorityId, context.ref.bindingRef).resolve(
            ref, request=request
        )

    async def _material_authorization(self, context, operation):
        from ksadk.kernel.teams_material_transport import MaterialHTTPAuthorization

        barrier = await self.runtime.kernel.get_execution_grant(context.grant)
        if barrier is None:
            permit = self.prepare_permits.get(context.context_ref)
            if operation != "read_material" or permit is None:
                raise PermitError("material preparation has no original execution permission")
            self._host(
                context.ref.authorityId, context.ref.bindingRef, clock=await self._database_clock()
            )._verify(
                context,
                permit,
                "prepare",
                revision=permit.grantRevision,
                expires_at=permit.expiresAt,
            )
            header = "X-Teams-Execution-Permit"
        else:
            permit = await self._callback(context, operation="invoke")
            header = "X-Teams-Callback-Permit"
        return MaterialHTTPAuthorization(headers={header: permit.model_dump_json()})

    async def policy(self, context, *, request):
        from ksadk.harness.execution_policy import ExecutionPolicy
        from ksadk.kernel.teams_tool_runtime import assemble_teams_tools

        await self._callback(context, operation="policy")
        value = context.context.get("policy")
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("systemContext"), str)
            or set(value)
            - {"systemContext", "childSystemContext", "limits", "approvalRequired", "effects"}
        ):
            raise ContextConflict("trusted execution policy unavailable")
        host = self._host(context.ref.authorityId, context.ref.bindingRef)

        async def invoke(operation, arguments, call_id):
            permit = await self._callback(context, operation="invoke")
            return await self._request(
                "/runtime/contexts/invoke",
                callback=True,
                permit=permit,
                body={
                    "contextRef": context.context_ref,
                    "commandId": context.ref.commandId,
                    "operation": operation,
                    "arguments": arguments,
                    "callId": call_id,
                },
            )

        async def send_effect(operation, value):
            permit = await self._callback(context, operation="invoke")
            return await self._request(
                f"/runtime/effects/{operation}",
                callback=True,
                permit=permit,
                body=value.model_dump(mode="json"),
            )

        workspace, tools = await assemble_teams_tools(
            context=context,
            host=host,
            request=request,
            workspace_root=self.config.state_dir / "workspaces",
            materializer=self.materializer,
            material_transport=self.material_transport,
            adapters=self.effect_adapters,
            invoke=invoke,
            send_effect=send_effect,
        )
        return ExecutionPolicy(
            system_context=value["systemContext"],
            child_system_context=value.get("childSystemContext"),
            tools=tools,
            workspace_root=workspace,
            exclusive_tools=True,
            limits=value.get("limits", {}),
            approval_required=frozenset(value.get("approvalRequired", [])),
        )

    async def close(self):
        await self.material_transport.close()
        if self.effect_journal is not None:
            await self.effect_journal.close()
        if self.owns_client:
            await self.client.aclose()
