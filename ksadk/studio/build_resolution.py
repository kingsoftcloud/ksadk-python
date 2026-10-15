"""Resolve immutable Studio Builds shared by Web and Scheduler admission."""

import asyncio
import json
import zipfile

from ksadk.studio.a2a_bindings import bindings_digest
from ksadk.studio.contracts import BuildStatus
from ksadk.studio.errors import StudioError
from ksadk.studio.resource_build_admission import resource_build_required


def _locked_definition_matches(studio, record, expected_digest: str) -> bool:
    if not record.artifact_path:
        return False
    try:
        path = studio.workspace.resolve(record.artifact_path, must_exist=True)
        with zipfile.ZipFile(path) as archive:
            lock = json.loads(archive.read("agentkit.lock"))
        return isinstance(lock, dict) and lock.get("definitionDigest") == expected_digest
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return False


async def resolve_current_build(studio, agent_id: str):
    """Return the Build that is eligible for a local Studio turn.

    This is deliberately one shared resolver for Web chat and Scheduler
    authoring.  A Scheduler must never select a mutable Draft or a stale
    Codex manifest merely because a UI happened to show it as an Agent.
    """

    await studio.start()
    if studio.is_codex_agent(agent_id):
        studio.codex_agent_detail(agent_id)
        manifest_snapshot = studio.codex_manifests.load(agent_id)
        await studio.a2a_catalog.validate_bindings(manifest_snapshot.manifest.remote_agents or [])
        cached = studio._current_codex_builds.get(agent_id)
        if cached is not None and cached.manifest_sha256 == manifest_snapshot.manifest_sha256:
            try:
                return studio.codex_builds.get(cached.id)
            except StudioError:
                studio._current_codex_builds.pop(agent_id, None)
        builds = [
            record
            for record in studio.codex_builds.list()
            if record.agent_name == agent_id and studio.codex_builder.is_current(record)
        ]
        if builds:
            studio._current_codex_builds[agent_id] = builds[0]
            return builds[0]
        built = await asyncio.to_thread(studio.codex_builder.build, agent_id)
        studio._current_codex_builds[agent_id] = built
        return built
    draft = studio.drafts.get(agent_id)
    await studio.a2a_catalog.validate_bindings(draft.spec.bindings.remote_agents)
    from ksadk.studio.a2a_orchestration import compile_orchestration

    a2a_composition = compile_orchestration(draft)
    a2a_composition_digest = a2a_composition.digest if a2a_composition else None
    composition_required = studio.plugin_compositions.required_for(draft)
    resources_required = resource_build_required(draft)
    # SDK catalog definitions can change without a Draft revision (e.g. a
    # corrected builtin argument schema). Keep each existing Build immutable,
    # but select a fresh Build for new turns when a bound definition changed.
    # Agents without builtin bindings keep their existing admission path.
    builtin_bindings = any(
        binding.enabled and binding.resource_id.startswith("tool:builtin:")
        for binding in draft.spec.bindings.tools
    )
    current_definition_digest = None
    for record in studio.builds.list_for_agent(agent_id):
        if record.status == BuildStatus.SUCCEEDED:
            if record.a2a_orchestration_digest != a2a_composition_digest:
                continue
            if record.a2a_binding_digest != bindings_digest(draft.spec.bindings.remote_agents):
                continue
            # Every runtime type is revision-bound.  Framework Builds are
            # just as immutable as composed Harness Builds and must never
            # be reused after the Agent draft revision changes.
            if record.source_revision != draft.metadata.revision:
                continue
            if composition_required and not studio._build_has_composition(record):
                continue
            if resources_required and not studio._build_has_resources(record):
                continue
            if builtin_bindings:
                if current_definition_digest is None:
                    compiled = await asyncio.to_thread(studio.builder.compiler.compile, draft)
                    current_definition_digest = compiled.resolved.resolved_digest
                if not _locked_definition_matches(studio, record, current_definition_digest):
                    continue
            if composition_required:
                composition = studio.plugin_compositions.compile_if_required(draft)
                if composition is None:  # pragma: no cover - guarded by required_for
                    raise StudioError(
                        "PLUGIN_COMPOSITION_REQUIRED",
                        "当前 Agent 需要插件组合，但无法生成 Composition",
                        status_code=409,
                    )
                if not studio._build_has_composition(record, composition=composition):
                    continue
                studio.plugin_compositions.bind_build(
                    composition,
                    agent_id=draft.metadata.id,
                    build_id=record.id,
                )
            return record
    if draft.spec.model is None and not draft.spec.bindings.model_profile_id:
        raise StudioError(
            "AGENT_MODEL_REQUIRED",
            "当前 Agent 未绑定 Model Profile，请先在 Agent 配置中选择模型；"
            "API Key 只提供访问凭证，不会自动绑定模型。",
            status_code=422,
            field="spec.bindings.modelProfileId",
        )
    return await asyncio.to_thread(studio._build_agent_bundle, draft)
