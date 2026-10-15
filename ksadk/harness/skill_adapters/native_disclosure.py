"""Expose only host-bound Skill metadata and progressive reads to Native.

Installation and version selection belong to the host/Skill Service. This
adapter cannot discover arbitrary filesystem skills or install a new one.
"""

from __future__ import annotations

import base64

from ksadk.harness.native.adapters import StrictToolExecutor, ToolAuthorizer, ToolBinding
from ksadk.harness.native.ports import (
    RunConfiguration,
    ToolDefinition,
    ToolPort,
    ToolRequest,
    ToolResult,
)
from ksadk.harness.native.state import RunScope
from ksadk.harness.skill_runtime import (
    SKILL_CATALOG_TOOL,
    SKILL_INSTRUCTIONS_TOOL,
    SKILL_MANIFEST_TOOL,
    SKILL_RESOURCE_TOOL,
    SkillDisclosureError,
    SkillRuntime,
    SkillSource,
)
from ksadk.harness.stores.native_contract import RunStore, StoreConflict

_CATALOG = ToolDefinition(
    name=SKILL_CATALOG_TOOL,
    read_only=True,
    context_retention="persistent",
    description="List the Skills bound to this Agent revision. Unlisted Skills are unavailable.",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "additionalProperties": False,
    },
)
_READS = tuple(
    ToolDefinition(
        name=tool.name,
        read_only=True,
        context_retention="persistent",
        description=tool.description,
        parameters=tool.parameters,
    )
    for tool in SkillRuntime.disclosure_tools()
    if tool.name in {SKILL_MANIFEST_TOOL, SKILL_INSTRUCTIONS_TOOL, SKILL_RESOURCE_TOOL}
)
DEFINITIONS = (_CATALOG, *_READS)
_BY_NAME = {definition.name: definition for definition in DEFINITIONS}


class NativeSkillDisclosurePort:
    """Run-scoped L0–L3 disclosure on an immutable host allowlist."""

    def __init__(
        self,
        *,
        delegate: ToolPort,
        scope: RunScope,
        source: SkillSource,
        skill_refs: tuple[str, ...],
        authorize: ToolAuthorizer,
        store: RunStore | None = None,
    ) -> None:
        if len(set(skill_refs)) != len(skill_refs) or any(not ref.strip() for ref in skill_refs):
            raise ValueError("native_skill_refs_invalid")
        self.delegate = delegate
        self.scope = scope
        self.skill_refs = skill_refs
        self.runtime = SkillRuntime(source)
        self.store = store
        self._restored_runs: set[str] = set()
        self._skills = StrictToolExecutor(
            scope=scope,
            tools=(
                ToolBinding(_CATALOG, self._catalog),
                ToolBinding(_BY_NAME[SKILL_MANIFEST_TOOL], self._manifest),
                ToolBinding(_BY_NAME[SKILL_INSTRUCTIONS_TOOL], self._instructions),
                ToolBinding(_BY_NAME[SKILL_RESOURCE_TOOL], self._resource),
            ),
            authorize=authorize,
        )

    async def execute(self, request: ToolRequest) -> ToolResult:
        name = request.call.name
        if name in _BY_NAME:
            if request.scope != self.scope:
                return await self._skills.execute(request)
            if self.store is not None:
                try:
                    self._restore_run(request.run_id)
                except (KeyError, ValueError, StoreConflict):
                    return ToolResult(status="error", content={"code": "skill_replay_unavailable"})
            return await self._skills.execute(request)
        return await self.delegate.execute(request)

    async def reconcile(self, request: ToolRequest) -> ToolResult:
        from ..native.tool_recovery import query_sealed_tool_result

        return await query_sealed_tool_result(self.delegate, request, owned_names=_BY_NAME)

    async def recover_prepared(self, request: ToolRequest) -> ToolResult:
        from ..native.tool_recovery import query_sealed_tool_result

        return await query_sealed_tool_result(
            self.delegate, request, owned_names=_BY_NAME, method="recover_prepared"
        )

    async def resume_prepared(self, request: ToolRequest) -> ToolResult:
        from ..native.tool_recovery import query_sealed_tool_result

        return await query_sealed_tool_result(
            self.delegate, request, owned_names=_BY_NAME, method="resume_prepared"
        )

    async def resume_checkpoint(self, request: ToolRequest) -> ToolResult:
        from ..native.tool_recovery import query_sealed_tool_result

        return await query_sealed_tool_result(
            self.delegate, request, owned_names=_BY_NAME, method="resume_checkpoint"
        )

    def _restore_run(self, run_id: str) -> None:
        if run_id in self._restored_runs or self.store is None:
            return
        snapshot = self.store.load(self.scope, run_id)
        if snapshot.scope != self.scope:
            raise ValueError("skill_replay_scope_mismatch")
        for event in self.store.events(self.scope, run_id):
            if event.kind != "effect.prepared" or event.payload.get("kind") != "tool":
                continue
            key = event.payload.get("operation_key")
            if not isinstance(key, str):
                continue
            intent = self.store.get_intent(self.scope, run_id, key)
            receipt = self.store.get_receipt(self.scope, run_id, key)
            if intent is None or receipt is None or receipt.status != "committed":
                continue
            call = intent.payload.get("call")
            result = receipt.result
            if not isinstance(call, dict) or result.get("status") != "ok":
                continue
            arguments = call.get("arguments")
            content = result.get("content")
            if not isinstance(arguments, dict) or not isinstance(content, dict):
                continue
            skill_id = arguments.get("skill_id")
            if (
                not isinstance(skill_id, str)
                or skill_id not in self.skill_refs
                or content.get("skill_id") != skill_id
            ):
                continue
            level = {
                SKILL_MANIFEST_TOOL: 1,
                SKILL_INSTRUCTIONS_TOOL: 2,
                SKILL_RESOURCE_TOOL: 3,
            }.get(call.get("name"))
            if level is None:
                continue
            try:
                self.runtime.restore_level(run_id, skill_id, level)
            except SkillDisclosureError:
                # A contradictory receipt sequence must not grant a later
                # disclosure level. The next live request remains denied.
                continue
        self._restored_runs.add(run_id)

    async def _catalog(self, request: ToolRequest) -> ToolResult:
        arguments = request.call.arguments
        query = arguments.get("query", "")
        if not isinstance(query, str):
            return ToolResult(status="error", content={"code": "invalid_skill_query"})
        try:
            return ToolResult(
                status="ok",
                content={
                    "bound_skills": list(self.runtime.recommend(self.skill_refs, query=query)),
                    "unlisted_skills_available": False,
                },
            )
        except Exception:
            return ToolResult(status="error", content={"code": "skill_source_unavailable"})

    async def _manifest(self, request: ToolRequest) -> ToolResult:
        arguments = request.call.arguments
        skill_id = arguments.get("skill_id")
        if not isinstance(skill_id, str) or skill_id not in self.skill_refs:
            return ToolResult(status="error", content={"code": "skill_not_bound"})
        try:
            manifest = self.runtime.level1(request.run_id, skill_id)
            return ToolResult(
                status="ok",
                content={
                    "skill_id": skill_id,
                    "name": manifest.name,
                    "summary": manifest.summary,
                    "conditions": manifest.conditions,
                    "required_tools": list(manifest.required_tools),
                    "resource_base": "skill-root",
                    "resource_access": (
                        "locked_package" if manifest.resource_refs is not None
                        else "instructions_only"
                    ),
                },
            )
        except Exception:
            return ToolResult(status="error", content={"code": "skill_source_unavailable"})

    async def _instructions(self, request: ToolRequest) -> ToolResult:
        skill_id = request.call.arguments.get("skill_id")
        if not isinstance(skill_id, str) or skill_id not in self.skill_refs:
            return ToolResult(status="error", content={"code": "skill_not_bound"})
        try:
            return ToolResult(
                status="ok",
                content={
                    "skill_id": skill_id,
                    "instructions": self.runtime.level2(request.run_id, skill_id),
                },
            )
        except SkillDisclosureError:
            return ToolResult(status="error", content={"code": "skill_disclosure_order_invalid"})
        except Exception:
            return ToolResult(status="error", content={"code": "skill_source_unavailable"})

    async def _resource(self, request: ToolRequest) -> ToolResult:
        arguments = request.call.arguments
        skill_id = arguments.get("skill_id")
        resource_ref = arguments.get("resource_ref")
        if not isinstance(skill_id, str) or skill_id not in self.skill_refs:
            return ToolResult(status="error", content={"code": "skill_not_bound"})
        if not isinstance(resource_ref, str) or not resource_ref:
            return ToolResult(status="error", content={"code": "invalid_skill_resource_ref"})
        if self.runtime.level(request.run_id, skill_id) < 2:
            return ToolResult(status="error", content={"code": "skill_disclosure_order_invalid"})
        try:
            content = self.runtime.level3(request.run_id, skill_id, resource_ref)
        except SkillDisclosureError:
            return ToolResult(status="error", content={"code": "skill_resource_unavailable"})
        except Exception:
            return ToolResult(status="error", content={"code": "skill_source_unavailable"})
        # Keep one resource safely inside the normal text result budget. The
        # ArtifactPort can handle larger output only when the host supplies it.
        if len(content) > 32_768:
            return ToolResult(
                status="error",
                content={"code": "skill_resource_too_large", "limit_bytes": 32_768},
            )
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult(
                status="ok",
                content={
                    "skill_id": skill_id,
                    "resource_ref": resource_ref,
                    "encoding": "base64",
                    "data": base64.b64encode(content).decode("ascii"),
                },
            )
        return ToolResult(
            status="ok",
            content={
                "skill_id": skill_id,
                "resource_ref": resource_ref,
                "encoding": "utf-8",
                "text": text,
            },
        )

    def clear_run(self, run_id: str) -> None:
        self.runtime.clear_run(run_id)


def install_native_skill_tools(
    configuration: RunConfiguration,
    delegate: ToolPort,
    *,
    scope: RunScope,
    source: SkillSource,
    skill_refs: tuple[str, ...],
    authorize: ToolAuthorizer,
    store: RunStore | None = None,
) -> tuple[RunConfiguration, NativeSkillDisclosurePort]:
    conflicts = {item.name for item in configuration.tools}.intersection(_BY_NAME)
    if conflicts:
        raise ValueError("reserved_native_skill_tool_conflict")
    tools = NativeSkillDisclosurePort(
        delegate=delegate,
        scope=scope,
        source=source,
        skill_refs=skill_refs,
        authorize=authorize,
        store=store,
    )
    # An empty host binding has nothing to disclose beyond the catalog. Do
    # not advertise read tools that can only return skill_not_bound; policy
    # checks in the port still reject a forged call to them.
    definitions = DEFINITIONS if skill_refs else (_CATALOG,)
    return configuration.model_copy(update={"tools": (*configuration.tools, *definitions)}), tools
