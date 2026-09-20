"""Trusted Teams tool assembly; no model-provided adapters or authority.

Only an explicitly installed, versioned external adapter may cross the effect
boundary. Ordinary shell/MCP tools are not adapted implicitly. Journal identity
is pinned beside the original Kernel context so a missing journal cannot be
silently provisioned on restart.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

from ksadk.kernel.teams_effects import (
    EffectExecutor,
    EffectOutcome,
    EffectPreparedReceipt,
    EffectReportReceipt,
    EffectRequest,
    PostgresEffectJournal,
    SQLiteEffectJournal,
    effect_payload_digest,
)
from ksadk.kernel.teams_execution_context import ContextConflict, StoreIdentityMismatch
from ksadk.plugins.teams.effect_contracts import ExecutionEffectsPage as ExecutionEffectsPage


async def open_effect_journal(registry):
    """Provision once explicitly; pin the journal UUID in the Kernel database."""
    incarnation = await registry.incarnation()
    original = await registry.extension_identity("effect-journal", expected_incarnation=incarnation)
    if registry.shared_across_hosts:
        journal = await PostgresEffectJournal.open(
            registry.kernel_store,
            store_incarnation=incarnation,
            initialize=original is None,
        )
    else:
        journal = SQLiteEffectJournal(
            registry.kernel_store.db_path.with_suffix(".teams-effects.sqlite"),
            store_incarnation=incarnation,
            initialize=original is None,
        )
    try:
        await registry.bind_extension_identity(
            "effect-journal",
            journal.journal_incarnation,
            expected_incarnation=incarnation,
        )
        return journal
    except BaseException:
        await journal.close()
        raise


@dataclass(frozen=True)
class TrustedEffectAdapter:
    description: str
    parameters: dict
    adapter_version: str
    effect_class: Literal["external_idempotent", "external_reconcilable"]
    execute: Callable[[dict, str], Awaitable[EffectOutcome]]

    def __post_init__(self):
        if (
            not self.adapter_version
            or not isinstance(self.parameters, dict)
            or self.effect_class not in {"external_idempotent", "external_reconcilable"}
            or not callable(self.execute)
        ):
            raise ValueError("invalid trusted effect adapter")


def adapter_capabilities(adapters):
    return {
        name: {"adapterVersion": adapter.adapter_version, "effectClass": adapter.effect_class}
        for name, adapter in sorted(adapters.items())
    }


class TeamsEffectTools:
    def __init__(self, *, context, host, journal, adapters, authorize, send):
        self.context, self.host, self.journal = context, host, journal
        self.adapters, self.authorize, self.send = dict(adapters), authorize, send

    def tools(self):
        from ksadk.harness.tools import HarnessTool

        policy = self.context.context.get("policy") or {}
        selected = policy.get("effects", {})
        if not isinstance(selected, dict):
            raise ContextConflict("invalid trusted effects policy")
        available = adapter_capabilities(self.adapters)
        if any(
            name not in available or value != available[name] for name, value in selected.items()
        ):
            raise ContextConflict("trusted effect adapter differs from frozen policy")
        result = {}
        for name in selected:
            adapter = self.adapters[name]

            async def call(arguments, call_id, *, name=name, adapter=adapter):
                if not isinstance(call_id, str) or not call_id:
                    raise ContextConflict("effect tool requires stable framework call identity")
                await self.authorize(self.context, name)
                incarnation = await self.host.registry.incarnation()
                if incarnation != self.journal.store_incarnation:
                    raise StoreIdentityMismatch("effect journal belongs to another Kernel store")
                receipt = await self.host._lookup(self.context, incarnation)
                run = await self.host.store.load_run(receipt.runId) if receipt.runId else None
                if receipt.status != "accepted" or run is None:
                    raise ContextConflict("effect requires the original accepted native Run")
                request = EffectRequest(
                    ref=self.context.ref,
                    context_ref=self.context.context_ref,
                    store_incarnation=incarnation,
                    journal_incarnation=self.journal.journal_incarnation,
                    native_run_id=receipt.runId,
                    tool_call_id=call_id,
                    effect_index=0,
                    tool_name=name,
                    adapter_version=adapter.adapter_version,
                    effect_class=adapter.effect_class,
                    payload_digest=effect_payload_digest(name, adapter.adapter_version, arguments),
                )

                async def authorize(_):
                    await self.authorize(self.context, name)

                async def prepare(value):
                    return EffectPreparedReceipt.model_validate(await self.send("prepare", value))

                record = await EffectExecutor(
                    self.journal,
                    authorize=authorize,
                    prepare_remote=prepare,
                ).invoke(request, arguments, adapter)
                # Report is an independent durable outbox: an uncertain ACK must
                # never turn a completed local effect into another external send.
                try:
                    for pending in await self.journal.pending_reports(limit=100):
                        if pending.request.context_ref != self.context.context_ref:
                            continue
                        ack = EffectReportReceipt.model_validate(await self.send("report", pending))
                        if (ack.effectKey, ack.journalRevision, ack.evidenceDigest) != (
                            pending.request.effect_key,
                            pending.revision,
                            pending.evidence_digest,
                        ):
                            raise ContextConflict("effect receipt identity mismatch")
                        await self.journal.acknowledge_report(
                            pending.request.effect_key,
                            pending.revision,
                            pending.evidence_digest,
                        )
                except Exception:
                    pass  # The journal retains the original report for recovery.
                return {
                    "effectKey": request.effect_key,
                    "phase": record.phase,
                    "outcome": record.outcome.model_dump(mode="json") if record.outcome else None,
                }

            result[name] = HarnessTool(
                name, adapter.description, adapter.parameters, call, "teams-effect"
            )
        return result


async def assemble_teams_tools(
    *,
    context,
    host,
    request,
    workspace_root,
    materializer,
    material_transport,
    adapters,
    invoke,
    send_effect,
):
    """Compose only bounded local tools, Server Teams methods and trusted adapters."""
    from ksadk.harness.tools import HarnessTool
    from ksadk.kernel.teams_artifact_tools import TeamsArtifactTools
    from ksadk.kernel.teams_workspace import prepare_workspace, workspace_tools
    from ksadk.plugins.teams.tool_service import TeamsToolMethods

    async def authorize(ctx, operation):
        await host.require_live_context(ctx.context_ref, request=request)

    prepared = manifest = None
    if context.material_manifest_ref:
        prepared = await materializer.materialize(context)
        from ksadk.kernel.teams_materials import ReadyMaterial

        manifest = ReadyMaterial.model_validate(
            await material_transport.fetch_manifest(
                context,
                context.material_manifest_ref,
            )
        ).manifest
    workspace = prepare_workspace(
        workspace_root,
        context,
        prepared_material=prepared,
        manifest=manifest,
    )

    async def mapped(call):
        from ksadk.plugins.teams.errors import TeamsError

        deadline = time.monotonic() + 5
        while True:
            await authorize(context, "original_run_mapping")
            try:
                return await call()
            except TeamsError as error:
                if (
                    error.status != 409
                    or error.code != "original_run_unverified"
                    or time.monotonic() >= deadline
                ):
                    raise
                await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))

    original_invoke, original_send_effect = invoke, send_effect

    async def invoke(operation, arguments, call_id):
        return await mapped(lambda: original_invoke(operation, arguments, call_id))

    async def send_effect(operation, value):
        return await mapped(lambda: original_send_effect(operation, value))

    tools = workspace_tools(workspace, context=context, authorize=authorize)
    for name, description, parameters in TeamsToolMethods.tool_definitions():

        async def call(arguments, call_id, *, operation=name):
            if not isinstance(call_id, str) or not call_id:
                raise ContextConflict("team tool requires stable call identity")
            await authorize(context, operation)
            return await invoke(operation, arguments, call_id)

        tools[name] = HarnessTool(name, description, parameters, call, "teams")
    artifact_tools = TeamsArtifactTools(
        context,
        workspace=workspace,
        transport=material_transport,
        invoke=invoke,
        authorize=authorize,
        prepared_material=prepared,
    ).tools()
    for name, tool in artifact_tools.items():

        async def artifact_call(arguments, call_id, *, tool=tool):
            return await mapped(lambda: tool.call(arguments, call_id=call_id))

        tools[name] = HarnessTool(
            name, tool.description, tool.parameters, artifact_call, "teams-artifact"
        )
    effects = TeamsEffectTools(
        context=context,
        host=host,
        journal=host.effect_journal,
        adapters=adapters,
        authorize=authorize,
        send=send_effect,
    ).tools()
    if set(effects) & set(tools):
        raise ContextConflict("effect adapter shadows a controlled tool")
    tools.update(effects)
    return workspace, tools
