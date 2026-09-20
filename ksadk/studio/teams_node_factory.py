"""Production outbound Node composition over fixed Studio Build Kernels.

Credentials, preparations and public verification keys come from the configured
Server. This module contains no issuer/private key and never reconstructs a
missing original execution. The old Studio policy resolver remains available
for ordinary local runs; Teams runs always use their persisted Host context.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ksadk.harness.execution_policy import ExecutionPolicy
from ksadk.kernel.authorization import AgentControlPermitVerifier, b64url_decode
from ksadk.kernel.execution_host_ingress import (
    HostBinding,
    KernelTeamsExecutionHost,
    TrustedGrantWindow,
    TrustedPreparation,
)
from ksadk.kernel.teams_execution_context import (
    ContextConflict,
    PreparedTeamsContext,
    SQLiteTeamsExecutionContextRegistry,
    StoreIdentityMismatch,
)
from ksadk.plugins.teams.cloud_contracts import NodeProbeCommand
from ksadk.plugins.teams.cloud_permits import TeamsPermitVerifier
from ksadk.studio.kernel_registry import StudioBuildKernelRegistry
from ksadk.studio.teams_catalog import StudioTeamsCatalog
from ksadk.studio.teams_node_v1 import KernelTeamsNodeExecutor, TeamsNodeV1

_CURRENT_COMMAND = ContextVar("teams_node_claim", default=None)


def _clock(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ContextConflict("trusted Server time requires a timezone")
    return value


class ServerVerificationKeys:
    """Only the authenticated, configured Server URL can supply verifier keys."""

    def __init__(self, node, *, issuer):
        self.node, self.issuer = node, issuer
        self.keys = {}

    async def refresh(self):
        data = await self.node._request("GET", "/jwks")
        values = {}
        for key in data.get("keys", []):
            if (
                key.get("kty") != "OKP"
                or key.get("crv") != "Ed25519"
                or not isinstance(key.get("kid"), str)
                or not key.get("x")
            ):
                raise ContextConflict("invalid Server verification key")
            if key["kid"] in values:
                raise ContextConflict("duplicate Server verification key")
            Ed25519PublicKey.from_public_bytes(b64url_decode(key["x"]))
            values[key["kid"]] = key["x"]
        if not values:
            raise ContextConflict("Server verification keys unavailable")
        self.keys = values

    async def fetch_verification_keys(self):
        await self.refresh()
        return dict(self.keys)

    def teams_verifier(self):
        return TeamsPermitVerifier(
            {
                kid: Ed25519PublicKey.from_public_bytes(b64url_decode(key))
                for kid, key in self.keys.items()
            },
            issuer=self.issuer,
        )


class StudioNodeHosts:
    def __init__(
        self,
        *,
        studio,
        node,
        authority_id,
        tenant_id,
        state_dir,
        keys,
        effect_adapters=None,
        loaded_build_factory=None,
    ):
        self.studio, self.node = studio, node
        self.authority_id, self.tenant_id = authority_id, tenant_id
        self.state_dir, self.keys = Path(state_dir), keys
        self.templates, self.hosts = {}, {}
        self.effect_adapters = dict(effect_adapters or {})
        self.loaded_build_factory = loaded_build_factory
        self.journals = {}
        from ksadk.kernel.teams_material_transport import TeamsHTTPMaterialTransport
        from ksadk.kernel.teams_materials import TeamsMaterializer

        self.material_transport = TeamsHTTPMaterialTransport(
            node.owner_client.base_url,
            node_id=node.identity["nodeId"],
            authorization=self._material_authorization,
        )
        self.materializer = TeamsMaterializer(
            self.state_dir / "materials",
            fetch_manifest=self.material_transport.fetch_manifest,
            open_blob=self.material_transport.open_blob,
        )
        self.prior_resolver = studio.plugin_runs.execution_policy_resolver
        self.registry = StudioBuildKernelRegistry(
            resolve_build=studio.resolve_run_spec,
            resolve_adapter_provider=studio._scheduler_adapter_provider,
            session_service=studio.session_service,
            runtime_executor=studio.runtime_executor,
            state_dir=self.state_dir / "runtime",
            tenant_id=tenant_id,
            workspace_id=node.identity["nodeId"],
            instance_namespace=node.identity["nodeId"],
            configure_runtime=self._configure_runtime,
        )
        # Must be mounted before provider activation snapshots capabilities and
        # before recovery workers can acquire any persisted invocation.
        studio.plugin_runs.execution_policy_resolver = self

    async def _configure_runtime(self, runtime, spec, build_id):
        store = runtime.kernel_store
        await store.ensure_schema()
        registry = SQLiteTeamsExecutionContextRegistry(store)
        await registry.initialize()
        from ksadk.kernel.teams_tool_runtime import open_effect_journal

        journal = await open_effect_journal(registry)
        self.journals[build_id] = journal
        # Explicit Server-only native authority; no local issuer/JWKS merged.
        runtime.kernel._permit_verifier = AgentControlPermitVerifier(
            self.keys,
            nonce_store=registry,
        )
        ref = "local-build:" + build_id
        raw_bundle = str(spec.manifest_sha256 or "")
        bundle = raw_bundle if raw_bundle.startswith("sha256:") else "sha256:" + raw_bundle
        from pydantic import TypeAdapter

        from ksadk.plugins.teams.cloud_contracts import Digest

        TypeAdapter(Digest).validate_python(bundle)
        from ksadk.plugins.teams.cloud_contract_fingerprints import teams_cloud_contract_digest

        binding = HostBinding(
            authority_id=self.authority_id,
            binding_ref=ref,
            provider_ref=spec.request_config.get("provider_ref", spec.launch_context.runtime_type),
            agent_instance_id=runtime.config.agent_instance_id,
            target={
                "kind": "node",
                "nodeId": self.node.identity["nodeId"],
                "nodeGeneration": self.node.identity["nodeGeneration"],
            },
            bundle_digest=bundle,
            contract_digest=teams_cloud_contract_digest(),
        )

        async def ensure(context):
            session = await self.studio.session_service.get_session_metadata(context.ref.sessionId)
            if session is None:
                session = await self.studio.session_service.create_session(
                    agent_id=binding.agent_instance_id,
                    user_id=context.owner_subject,
                    session_id=context.ref.sessionId,
                )
            return session

        host = KernelTeamsExecutionHost(
            kernel=runtime.kernel,
            store=store,
            events=runtime.session_events,
            registry=registry,
            binding=binding,
            permit_verifier=self.keys.teams_verifier(),
            context_loader=self.load_context,
            session_ensurer=ensure,
            policy_factory=self.policy,
            material_preparer=self.materializer.prepare,
            effect_journal=journal,
            effect_adapters=self.effect_adapters,
            loaded_build_provider=(
                self.loaded_build_factory(runtime, spec, build_id)
                if self.loaded_build_factory is not None
                else None
            ),
            teams_tools_ready=True,
            callback_ready=True,
            policy_enforcement_ready=True,
            canonical_events_durable=True,
            canonical_events_shared=False,
        )
        self.templates[ref] = host
        # Expose only actual descriptor facts for native/runtime reporting.
        runtime.teams_host_descriptor = lambda: self.descriptor(host)

    async def descriptor(self, host):
        from ksadk.plugins.teams.cloud_contracts import digest

        capabilities = host.capability_snapshot()
        return {
            "capabilities": capabilities,
            "capabilitiesDigest": digest(capabilities),
            "bundleDigest": host.binding.bundle_digest,
            "contractDigest": host.binding.contract_digest,
            "agentInstanceId": host.binding.agent_instance_id,
            "storeIncarnation": await host.registry.incarnation(),
        }

    async def load_context(self, context_ref):
        command = _CURRENT_COMMAND.get()
        if command is None or isinstance(command, NodeProbeCommand):
            raise ContextConflict("trusted context loading requires a current claimed operation")
        started = time.monotonic()
        response = await self.node._request(
            "POST",
            f"/nodes/{self.node.identity['nodeId']}/contexts/resolve",
            {"nodeCommandId": command.nodeCommandId, "contextRef": context_ref},
        )
        context = PreparedTeamsContext.model_validate(response["context"])
        KernelTeamsNodeExecutor._same_ref(context.ref, command.ref)
        if context.context_ref != context_ref or context.command.tenant_id != self.tenant_id:
            raise ContextConflict("trusted preparation scope mismatch")
        matches = [
            h
            for h in self.templates.values()
            if h.binding.agent_instance_id == context.grant.agent_instance_id
        ]
        if len(matches) != 1:
            raise ContextConflict("prepared instance is not an advertised fixed Build")
        if await matches[0].registry.incarnation() != response["storeIncarnation"]:
            raise StoreIdentityMismatch("Server references a different original Kernel store")
        return TrustedPreparation(
            context,
            TrustedGrantWindow(
                response["expiresAt"],
                _clock(response["serverTime"]),
                started,
            ),
        )

    def _bound_host(self, template, binding_ref):
        key = (template.binding.agent_instance_id, binding_ref)
        host = self.hosts.get(key)
        if host is None:
            from copy import copy

            host = copy(template)
            host.binding = replace(template.binding, binding_ref=binding_ref)
            self.hosts[key] = host
        host.verifier = self.keys.teams_verifier()
        return host

    async def for_command(self, command):
        await self.keys.refresh()
        if isinstance(command, NodeProbeCommand):
            template = self.templates.get(command.localBindingRef)
            if template is None:
                raise ContextConflict("probe targets an unadvertised local Build")
            return self._bound_host(template, command.bindingRef)
        prior = self.node.execution(command.ref.commandId)
        context = None
        if prior:
            for template in self.templates.values():
                # A shared SQLite session DB has one incarnation, but every
                # context still names its exact Build instance.
                if await template.registry.incarnation() != prior["store_incarnation"]:
                    continue
                context = await template.registry.get(
                    prior["context_ref"],
                    expected_incarnation=prior["store_incarnation"],
                )
                if context:
                    break
        if context is None:
            if command.operation != "prepare":
                raise StoreIdentityMismatch("original preparation is unavailable")
            context = (await self.load_context(command.payload.contextRef)).context
        KernelTeamsNodeExecutor._same_ref(context.ref, command.ref)
        for template in self.templates.values():
            if template.binding.agent_instance_id == context.grant.agent_instance_id:
                return self._bound_host(template, context.ref.bindingRef)
        raise ContextConflict("execution instance is not an advertised fixed Build")

    async def resolve(self, ref, *, request):
        context_ref = request.metadata.get("teams_context_ref")
        if not context_ref:
            if self.prior_resolver is None:
                raise ContextConflict("execution policy resolver unavailable")
            return await self.prior_resolver.resolve(ref, request=request)
        for template in self.templates.values():
            context = await template.registry.get(
                context_ref,
                expected_incarnation=await template.registry.incarnation(),
            )
            if (
                context is not None
                and context.grant.agent_instance_id == template.binding.agent_instance_id
            ):
                return await self._bound_host(template, context.ref.bindingRef).resolve(
                    ref, request=request
                )
        raise StoreIdentityMismatch("original policy context unavailable")

    async def _material_authorization(self, context, operation):
        from ksadk.kernel.teams_material_transport import MaterialHTTPAuthorization

        current = _CURRENT_COMMAND.get()
        node_command_id = None
        if current is not None and not isinstance(current, NodeProbeCommand):
            node_command_id = current.nodeCommandId
        if node_command_id is None:
            row = self.node.db.execute(
                "SELECT id FROM node_v1_operations "
                "WHERE json_extract(body,'$.operation')='prepare' "
                "AND json_extract(body,'$.payload.contextRef')=? ORDER BY rowid LIMIT 1",
                (context.context_ref,),
            ).fetchone()
            node_command_id = row["id"] if row else None
        if node_command_id is None:
            raise ContextConflict("original prepare operation is unavailable")
        return MaterialHTTPAuthorization(
            headers={"X-Teams-Node-Token": self.node.identity["accessToken"]},
            node_command_id=node_command_id,
        )

    async def policy(self, context, *, request):
        from ksadk.kernel.teams_tool_runtime import assemble_teams_tools

        value = context.context.get("policy")
        if not isinstance(value, dict) or not isinstance(value.get("systemContext"), str):
            raise ContextConflict("trusted execution policy is unavailable")
        if set(value) - {
            "systemContext",
            "childSystemContext",
            "limits",
            "approvalRequired",
            "effects",
        }:
            raise ContextConflict("unsupported trusted execution policy field")
        template = next(
            (
                h
                for h in self.templates.values()
                if h.binding.agent_instance_id == context.grant.agent_instance_id
            ),
            None,
        )
        if template is None:
            raise ContextConflict("original Build runtime is unavailable")
        host = self._bound_host(template, context.ref.bindingRef)

        async def invoke(operation, arguments, call_id):
            return await self.node._request(
                "POST",
                f"/nodes/{self.node.identity['nodeId']}/contexts/invoke",
                {
                    "contextRef": context.context_ref,
                    "commandId": context.ref.commandId,
                    "operation": operation,
                    "arguments": arguments,
                    "callId": call_id,
                },
            )

        async def send_effect(operation, value):
            return await self.node._request(
                "POST",
                f"/nodes/{self.node.identity['nodeId']}/effects/{operation}",
                value.model_dump(mode="json"),
            )

        workspace, tools = await assemble_teams_tools(
            context=context,
            host=host,
            request=request,
            workspace_root=self.state_dir / "workspaces",
            materializer=self.materializer,
            material_transport=self.material_transport,
            adapters=self.effect_adapters,
            invoke=invoke,
            send_effect=send_effect,
        )
        return ExecutionPolicy(
            system_context=value["systemContext"],
            tools=tools,
            workspace_root=workspace,
            child_system_context=value.get("childSystemContext"),
            exclusive_tools=True,
            limits=value.get("limits", {}),
            approval_required=frozenset(value.get("approvalRequired", [])),
        )

    async def recover_effect_reports(self):
        from ksadk.kernel.teams_effects import drain_effect_reports

        async def report(record):
            return await self.node._request(
                "POST",
                f"/nodes/{self.node.identity['nodeId']}/effects/recovery",
                record.model_dump(mode="json"),
            )

        for journal in self.journals.values():
            await drain_effect_reports(journal, report)

    async def close(self):
        await self.material_transport.close()
        for journal in self.journals.values():
            await journal.close()
        await self.registry.close()
        if self.studio.plugin_runs.execution_policy_resolver is self:
            self.studio.plugin_runs.execution_policy_resolver = self.prior_resolver


class BoundNodeExecutor(KernelTeamsNodeExecutor):
    def __init__(self, hosts):
        super().__init__(hosts.for_command)
        self.hosts = hosts

    async def execute(self, command, **kwargs):
        token = _CURRENT_COMMAND.set(command)
        try:
            return await super().execute(command, **kwargs)
        finally:
            _CURRENT_COMMAND.reset(token)

    async def recover_effect_reports(self):
        await self.hosts.recover_effect_reports()

    async def close(self):
        await self.hosts.close()


async def create_studio_teams_node(
    *,
    studio,
    owner_client,
    state_dir,
    authority_id,
    name,
    node_transport=None,
    effect_adapters=None,
    loaded_build_factory=None,
):
    """RemoteStudioTeamsInstallation.node_v1_factory; no extra caller policy.

    The node registers with an empty catalog first, obtains its authenticated
    configuration, then advertises descriptors of actually mounted Kernels.
    Preparation and submit remain separate claimed Server operations.
    """
    from ksadk.sessions.local_service import LocalSessionService

    if not isinstance(studio.session_service, LocalSessionService):
        raise ContextConflict("outbound local Node requires a durable SQLite session service")
    node = TeamsNodeV1(
        owner_client,
        None,
        state_dir=state_dir,
        name=name,
        bindings=[],
        node_transport=node_transport,
    )
    hosts = None
    try:
        await node.register()
        config = await node._request("GET", f"/nodes/{node.identity['nodeId']}/configuration")
        if (
            config.get("protocolVersion") != "teams-node/v1"
            or config.get("authorityId") != authority_id
            or node.identity["authorityId"] != authority_id
            or not isinstance(config.get("tenantId"), str)
            or not config["tenantId"]
        ):
            raise ContextConflict("Server node configuration scope mismatch")
        environment = studio.configuration.environment()
        keys = ServerVerificationKeys(
            node, issuer=environment.get("KSADK_TEAMS_PERMIT_ISSUER", "agentengine-server")
        )
        await keys.refresh()
        hosts = StudioNodeHosts(
            studio=studio,
            node=node,
            authority_id=authority_id,
            tenant_id=config["tenantId"],
            state_dir=state_dir,
            keys=keys,
            effect_adapters=effect_adapters,
            loaded_build_factory=loaded_build_factory,
        )
        node.executor = BoundNodeExecutor(hosts)
        await hosts.registry.start()
        catalog = StudioTeamsCatalog(studio, authority_ref=authority_id)
        bindings = []
        for item in catalog._local():
            if item.get("buildId") is None or item["availability"]["state"] != "unchecked":
                continue
            await hosts.registry.ensure_build(item["buildId"], expected_agent_id=item["agentId"])
            host = hosts.templates[item["bindingRef"]]
            capabilities = host.capability_snapshot()
            if not capabilities["teamsReady"]:
                continue
            from ksadk.plugins.teams.cloud_contracts import digest

            matrix = host.kernel.capabilities()
            bindings.append(
                {
                    "localBindingRef": item["bindingRef"],
                    "agentId": item["agentId"],
                    "buildId": item["buildId"],
                    "providerRef": host.binding.provider_ref,
                    "name": item["name"],
                    "bundleDigest": host.binding.bundle_digest,
                    "contractDigest": host.binding.contract_digest,
                    "capabilitiesDigest": digest(capabilities),
                    "capabilities": {
                        "enqueue": True,
                        "leader": True,
                        "restore": matrix.durable_restore.supported,
                        "idempotentLookup": True,
                        "terminalEvidence": True,
                        "isolatedSession": True,
                        "executionGrant": True,
                        "toolPolicy": True,
                        "teamsTools": True,
                        "materialization": capabilities["materialPreparation"],
                        "effectLedger": capabilities["effectLedger"],
                    },
                }
            )
        from ksadk.studio.teams_node_v1 import normalized_node_bindings

        node.bindings = normalized_node_bindings(bindings)
        await node.sync_catalog()
        return node
    except BaseException:
        # Close even if failure precedes executor composition.
        if hosts is not None and node.executor is None:
            await hosts.close()
        await node.close()
        raise
