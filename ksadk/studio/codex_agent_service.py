"""Codex Agent lifecycle behind the common Studio Agent contract.

The deployable ``agentengine.yaml`` deliberately remains a narrow platform
contract.  Studio-only display metadata and bindings live in a sidecar draft so
editing a Codex Agent behaves like every other Runtime without polluting the
ManagedRuntime manifest.
"""

from __future__ import annotations

import asyncio
import builtins
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, cast

from pydantic import ValidationError

from ksadk.managed_runtime import installed_runtime_version
from ksadk.studio.codex_builder import CodexBuildRecord
from ksadk.studio.codex_manifest import (
    CodexAgentManifest,
    CodexManifestSnapshot,
    CodexRuntimeRef,
)
from ksadk.studio.contracts import (
    AgentAppearance,
    AgentBindings,
    AgentDraft,
    AgentMetadata,
    AgentSpec,
    CapabilityBinding,
    Instructions,
    ModelSpec,
    Operation,
    OperationKind,
    RunEvent,
    RunStatus,
    RuntimeRef,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.templates import default_agent_spec
from ksadk.studio.workspace import Workspace


class CodexDraftRepository:
    """Persist Studio-only Codex metadata separately from deployment YAML."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def _path(self, agent_id: str) -> Path:
        return self.workspace.resolve(Path(".agentkit/codex-drafts") / f"{agent_id}.json")

    def get(self, agent_id: str) -> AgentDraft | None:
        path = self._path(agent_id)
        if not path.is_file():
            return None
        try:
            return cast(
                AgentDraft,
                AgentDraft.model_validate_json(path.read_text(encoding="utf-8")),
            )
        except (OSError, ValidationError) as exc:
            raise StudioError(
                "CODEX_DRAFT_INVALID",
                "Codex Agent 的 Studio 元数据损坏",
                status_code=500,
                details={"agentId": agent_id},
            ) from exc

    def save(self, draft: AgentDraft) -> AgentDraft:
        payload = draft.model_dump(
            by_alias=True,
            exclude_none=True,
            mode="json",
        )
        self.workspace.atomic_write_text(
            self._path(draft.metadata.id),
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        return draft

    def delete(
        self,
        agent_id: str,
        *,
        purge: bool,
        trash_directory: Path | None,
    ) -> None:
        path = self._path(agent_id)
        if not path.is_file():
            return
        if purge:
            path.unlink()
            return
        if trash_directory is None:
            raise ValueError("recoverable deletion requires a trash directory")
        target = self.workspace.resolve(trash_directory / "metadata" / path.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))


class CodexAgentService:
    """Own Codex definition projection, metadata and operation dispatch."""

    def __init__(self, studio: Any) -> None:
        self.studio = studio
        self.drafts = getattr(studio, "codex_drafts", None) or CodexDraftRepository(
            studio.workspace
        )

    def manifest_state(self, agent_id: str | None = None) -> dict:
        snapshot = self.studio.codex_manifests.load(agent_id)
        builds = self._builds(snapshot.manifest.name)
        latest = builds[0] if builds else None
        return {
            "manifest": snapshot.manifest,
            "manifestSha256": snapshot.manifest_sha256,
            "sourcePath": self.studio.workspace.relative(snapshot.source_path),
            "sourceYaml": snapshot.source_bytes.decode("utf-8"),
            "buildCurrent": bool(latest and self.studio.codex_builder.is_current(latest)),
            "latestBuild": latest,
        }

    def save_manifest(self, manifest: CodexAgentManifest) -> dict:
        snapshot = self.studio.codex_manifests.save(manifest)
        current = self.drafts.get(manifest.name)
        if current is not None:
            projected = self._project(snapshot, current=current)
            projected.metadata.revision = current.metadata.revision + 1
            self.drafts.save(projected)
        return self.manifest_state(manifest.name)

    def list(self, *, query: str = "", limit: int = 50) -> list[AgentDraft]:
        if limit < 1:
            return []
        normalized = query.strip().lower()
        results: list[AgentDraft] = []
        for snapshot in self.studio.codex_manifests.list():
            draft = self._project(snapshot)
            if (
                normalized
                and normalized not in draft.metadata.id.lower()
                and (normalized not in draft.metadata.name.lower())
            ):
                continue
            results.append(draft)
        return results[:limit]

    def create(
        self,
        *,
        agent_id: str,
        spec: AgentSpec | None,
        name: str | None = None,
        labels: dict[str, str] | None = None,
    ) -> AgentDraft:
        if self.studio.codex_manifests.exists(agent_id) or self.studio._draft_exists(agent_id):
            raise StudioError(
                "AGENT_ALREADY_EXISTS",
                "本地 Agent 已存在",
                status_code=409,
                details={"agentId": agent_id},
            )
        resolved = (spec or default_agent_spec("blank")).model_copy(deep=True)
        resolved.runtime = RuntimeRef(type="codex", version=self._runtime_version(resolved))
        self.ensure_bindings_supported(resolved)
        manifest = self._manifest(agent_id, resolved)
        snapshot = self.studio.codex_manifests.save(manifest)
        draft = AgentDraft(
            metadata=AgentMetadata(
                id=agent_id,
                name=str(name or agent_id).strip() or agent_id,
                labels={**self._labels(manifest), **dict(labels or {})},
            ),
            spec=resolved,
        )
        self.drafts.save(draft)
        return self._project(snapshot)

    def update(
        self,
        agent_id: str,
        spec: AgentSpec,
        *,
        expected_revision: int,
        name: str | None = None,
    ) -> AgentDraft:
        snapshot = self.studio.codex_manifests.load(agent_id)
        current = self._project(snapshot)
        if current.metadata.revision != expected_revision:
            raise StudioError(
                "AGENT_REVISION_CONFLICT",
                "Agent 已被其他操作更新",
                status_code=409,
                details={
                    "expected": expected_revision,
                    "actual": current.metadata.revision,
                },
            )
        resolved = spec.model_copy(deep=True)
        resolved.runtime = current.spec.runtime
        self._ensure_mcp_bindings_supported(resolved)
        # 早期 Studio 曾把 ksadk Tool 写进 Codex 草稿，尽管 Codex Runtime
        # 从未执行这些绑定。允许原样保存这类 dormant 历史数据，避免用户只改
        # Prompt/Model 时被迫丢绑定；新增、删除或修改仍按当前能力矩阵拒绝。
        if (
            resolved.bindings.tools != current.spec.bindings.tools
            or resolved.capabilities.tools != current.spec.capabilities.tools
        ):
            self.ensure_bindings_supported(resolved)
        manifest = self._manifest(agent_id, resolved, current=snapshot.manifest)
        updated_snapshot = self.studio.codex_manifests.save(manifest)
        updated = AgentDraft(
            metadata=current.metadata.model_copy(
                deep=True,
                update={
                    "revision": current.metadata.revision + 1,
                    **({"name": name} if name is not None else {}),
                },
            ),
            spec=resolved,
        )
        updated.metadata.labels.update(self._labels(manifest))
        self.drafts.save(updated)
        return self._project(updated_snapshot)

    def update_appearance(
        self,
        agent_id: str,
        appearance: AgentAppearance,
        *,
        expected_revision: int,
    ) -> AgentDraft:
        snapshot = self.studio.codex_manifests.load(agent_id)
        current = self._project(snapshot)
        if current.metadata.revision != expected_revision:
            raise StudioError(
                "AGENT_REVISION_CONFLICT",
                "Agent 已被其他操作更新",
                status_code=409,
                field="metadata.revision",
                details={"expected": expected_revision, "actual": current.metadata.revision},
            )
        updated = current.model_copy(deep=True)
        updated.metadata.revision += 1
        updated.metadata.appearance = appearance
        self.drafts.save(updated)
        return self._project(snapshot, current=updated)

    def ensure_bindings_supported(self, spec: AgentSpec) -> None:
        bindings = spec.bindings
        if bindings.tools or spec.capabilities.tools:
            raise StudioError(
                "TOOL_RUNTIME_INCOMPATIBLE",
                "Codex Runtime 当前只使用其原生工具，不能绑定 ksadk Tool",
                status_code=422,
                field="spec.bindings.tools",
                details={"runtimeType": "codex"},
            )
        # codex 支持 streamable-http MCP（通过 config_overrides 注入 mcp_servers）。
        self._ensure_mcp_bindings_supported(spec)

    def _ensure_mcp_bindings_supported(self, spec: AgentSpec) -> None:
        direct_dynamic = [
            server.name
            for server in spec.capabilities.mcp_servers
            if server.enabled and server.materialization == "dsh-profile"
        ]
        if direct_dynamic:
            raise StudioError(
                "DSH_MCP_MANAGED_BINDING_REQUIRED",
                "DSH Profile MCP 必须从当前 Catalog Resource 显式绑定，不能直接写入 AgentSpec",
                status_code=422,
                field="spec.capabilities.mcpServers",
                details={"servers": sorted(direct_dynamic)},
            )
        for binding in spec.bindings.mcp_servers:
            if not binding.enabled:
                continue
            descriptor = self.studio.catalog.get(binding.resource_id)
            if descriptor.contract.get("materialization") == "dsh-profile":
                raise StudioError(
                    "DSH_MCP_RUNTIME_INCOMPATIBLE",
                    "DSH Profile MCP 当前只支持 Harness Runtime",
                    status_code=422,
                    field="spec.runtime.type",
                    details={"runtimeType": "codex"},
                )

    def _skill_resource_ids(self, spec: AgentSpec) -> list[str]:
        bindings = spec.bindings
        ids = [b.resource_id for b in bindings.skills]
        ids.extend(spec.capabilities.skills or [])
        seen: set[str] = set()
        ordered: list[str] = []
        for sid in ids:
            if sid and sid not in seen:
                seen.add(sid)
                ordered.append(sid)
        return ordered

    def _mcp_server_configs(self, spec: AgentSpec) -> list[dict[str, Any]]:
        """Resolve bound MCP resources to native Codex HTTP or stdio configs."""
        bindings = spec.bindings
        ids = [b.resource_id for b in bindings.mcp_servers]
        ids.extend(spec.capabilities.mcp_servers or [])
        seen: set[str] = set()
        ordered: list[dict[str, Any]] = []
        catalog = getattr(self.studio, "catalog", None)
        if catalog is None:
            return ordered
        try:
            resources = catalog.list(limit=200)
        except Exception:
            return ordered
        mcp_by_id = {item.resource_id: item for item in resources if item.kind == "mcp"}
        for rid in ids:
            if rid in seen:
                continue
            seen.add(rid)
            descriptor = mcp_by_id.get(rid)
            if descriptor is None:
                continue
            contract = descriptor.contract or {}
            if contract.get("materialization") == "dsh-profile":
                raise StudioError(
                    "DSH_MCP_RUNTIME_INCOMPATIBLE",
                    "DSH Profile MCP 不能物化为 Codex MCP 配置",
                    status_code=422,
                    field="spec.bindings.mcpServers",
                    details={"runtimeType": "codex"},
                )
            url = str(
                contract.get("endpointUrl")
                or contract.get("endpoint_url")
                or contract.get("url")
                or ""
            ).strip()
            transport = str(contract.get("transport") or ("http" if url else "")).lower()
            command = str(contract.get("command") or "").strip()
            if transport == "stdio" and command:
                entry: dict[str, Any] = {
                    "name": descriptor.name,
                    "transport": "stdio",
                    "command": command,
                    "args": [str(argument) for argument in (contract.get("args") or [])],
                }
            elif transport in {"http", "sse"} and url:
                entry = {
                    "name": descriptor.name,
                    "transport": transport,
                    "url": url,
                }
            else:
                continue
            env_refs = contract.get("envRefs") or {}
            if transport == "stdio" and isinstance(env_refs, dict) and env_refs:
                entry["env_refs"] = {
                    str(env_name): str(reference)
                    for env_name, reference in env_refs.items()
                }
            elif isinstance(env_refs, dict):
                env_key = ""
                for ref in env_refs.values():
                    if isinstance(ref, str) and ref.startswith("env://"):
                        env_key = ref.removeprefix("env://")
                        break
                if env_key:
                    entry["env_key"] = env_key
            ordered.append(entry)
        return ordered

    def delete(self, agent_id: str, *, purge: bool = False) -> None:
        self.studio.codex_manifests.load(agent_id)
        running = [
            run.id
            for run in self.studio.event_store.list_runs(agent_id=agent_id)
            if run.status in {RunStatus.CREATED, RunStatus.RUNNING}
        ]
        if running:
            raise StudioError(
                "AGENT_RUN_ACTIVE",
                "Agent 仍有运行中的任务，请等待运行结束后再删除",
                status_code=409,
                details={"agentId": agent_id, "runIds": running},
            )
        trash_directory = None
        if not purge:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            trash_directory = self.studio.workspace.resolve(
                Path(".agentkit/trash/agents") / f"{agent_id}-{timestamp}"
            )
            trash_directory.mkdir(parents=True, exist_ok=False)
        self.studio.event_store.delete_agent(
            agent_id,
            purge=purge,
            trash_directory=trash_directory,
        )
        self.studio.codex_builds.delete_for_agent(
            agent_id,
            purge=purge,
            trash_directory=trash_directory,
        )
        self.drafts.delete(
            agent_id,
            purge=purge,
            trash_directory=trash_directory,
        )
        # 早期 Studio 会把首个 Codex Agent 同时保存在根 agentengine.yaml 与
        # agents/<id>/agentengine.yaml。Repository.load() 会优先返回根文件；只删
        # 一次会让同一个 Agent 在刷新列表后从副本“复活”。最多消费这两个兼容
        # 位置，且始终使用同一 recoverable trash 目录。
        for _ in range(2):
            try:
                self.studio.codex_manifests.delete(
                    agent_id,
                    purge=purge,
                    trash_directory=trash_directory,
                )
            except StudioError as exc:
                if exc.status_code == 404:
                    break
                raise

    def detail(self, agent_id: str | None = None) -> dict:
        snapshot = self.studio.codex_manifests.load(agent_id)
        _mcp_bindings, unresolved_mcp = self._mcp_bindings(snapshot.manifest)
        builds_view: list[dict] = []
        for item in self._builds(snapshot.manifest.name):
            view = self.build_view(item)
            try:
                view["isCurrent"] = self.studio.codex_builder.is_current(item)
            except Exception:
                view["isCurrent"] = True
            builds_view.append(view)
        return {
            "draft": self._project(snapshot),
            "builds": builds_view,
            "validation": {"valid": True, "level": "build", "diagnostics": []},
            "manifestSha256": snapshot.manifest_sha256,
            "sourcePath": self.studio.workspace.relative(snapshot.source_path),
            "bindingProjection": {
                "unresolvedMcpServers": unresolved_mcp,
            },
        }

    @staticmethod
    def build_view(record: CodexBuildRecord) -> dict:
        payload = {
            "id": record.id,
            "agentId": record.agent_name,
            "sourceRevision": record.source_revision,
            "status": record.status,
            "resolvedDigest": record.manifest_sha256,
            "bundleDigest": f"sha256:{record.manifest_sha256}",
            "artifactPath": record.artifact_path,
            "diagnostics": [],
            "createdAt": record.created_at,
            "completedAt": record.created_at,
            "manifestSha256": record.manifest_sha256,
            "runtimeLock": record.runtime_lock,
            "runtimeName": record.runtime_name,
            "runtimeVersion": record.runtime_version,
            "proxyMode": record.proxy_mode,
        }
        if record.plugin_lock is not None:
            payload.update(
                {
                    "pluginLock": record.plugin_lock.model_dump(
                        by_alias=True, exclude_none=True, mode="json"
                    ),
                    "pluginLockDigest": record.plugin_lock_digest,
                    "pluginMarketplace": (
                        record.plugin_marketplace.model_dump(
                            by_alias=True, exclude_none=True, mode="json"
                        )
                        if record.plugin_marketplace is not None
                        else None
                    ),
                    "pluginRuntimeStatus": record.plugin_runtime_status or {},
                }
            )
        return payload

    def submit_build(
        self,
        *,
        idempotency_key: str,
        agent_id: str | None = None,
    ) -> Operation:
        resolved_id = self.studio.codex_manifests.load(agent_id).manifest.name
        revision = self._project(self.studio.codex_manifests.load(resolved_id)).metadata.revision

        async def runner(_operation_id: str):
            return await asyncio.to_thread(
                self.studio.codex_builder.build,
                resolved_id,
                source_revision=revision,
            )

        return cast(
            Operation,
            self.studio.operations.submit(
                kind=OperationKind.BUILD,
                resource_id=resolved_id,
                idempotency_key=idempotency_key,
                runner=runner,
            ),
        )

    def submit_run(
        self,
        build_id: str,
        user_input: str,
        *,
        session_id: str | None,
        model: str | None,
        idempotency_key: str,
        on_event: Callable[[RunEvent], None] | None,
        sandbox: str | None = None,
        approval_mode: str | None = None,
        collaboration_mode: str | None = None,
        goal_objective: str | None = None,
        reasoning_effort: str | None = None,
        runtime_input: Any = None,
    ) -> Operation:
        async def runner(_operation_id: str):
            return await self.studio.run_build(
                build_id,
                user_input,
                session_id,
                model=model,
                sandbox=sandbox,
                approval_mode=approval_mode,
                collaboration_mode=collaboration_mode,
                goal_objective=goal_objective,
                reasoning_effort=reasoning_effort,
                runtime_input=runtime_input,
                on_event=on_event,
            )

        return cast(
            Operation,
            self.studio.operations.submit(
                kind=OperationKind.RUN,
                resource_id=build_id,
                idempotency_key=idempotency_key,
                runner=runner,
            ),
        )

    def _project(
        self,
        snapshot: CodexManifestSnapshot,
        *,
        current: AgentDraft | None = None,
    ) -> AgentDraft:
        manifest = snapshot.manifest
        saved = current or self.drafts.get(manifest.name)
        bindings = self._model_bindings(manifest)
        mcp_bindings, _unresolved_mcp = self._mcp_bindings(manifest)
        skill_bindings = [
            CapabilityBinding(resource_id=resource_id)
            for resource_id in (manifest.skills or [])
        ]
        # 从 Manifest 恢复 PCM context/memory（方案 §5.1：Build 不可变）
        # Manifest 已在 model_validate 时严格校验；这里直接恢复
        from ksadk.studio.contracts import ContextSpec, MemorySpec

        context_spec = manifest.context or ContextSpec()
        memory_spec = manifest.memory or MemorySpec()
        if saved is not None:
            draft = saved.model_copy(deep=True)
            draft.spec.runtime = RuntimeRef(
                type="codex",
                version=manifest.runtime.version,
            )
            draft.spec.instructions = Instructions(
                system=manifest.prompt,
                task=manifest.task_prompt or "",
            )
            draft.spec.soul = manifest.soul
            draft.spec.bindings.model_profile_id = bindings[0]
            draft.spec.bindings.model_profile_ids = bindings[1]
            draft.spec.bindings.skills = skill_bindings
            draft.spec.bindings.mcp_servers = mcp_bindings
            draft.spec.bindings.plugins = list(manifest.plugins or [])
            draft.spec.context = context_spec
            draft.spec.memory = memory_spec
            draft.metadata.labels.update(self._labels(manifest))
            return draft
        default_profile, profiles = bindings
        return AgentDraft(
            metadata=AgentMetadata(
                id=manifest.name,
                name=manifest.name,
                labels=self._labels(manifest),
            ),
            spec=AgentSpec(
                description="由 agentengine.yaml 管理的 Codex Agent",
                runtime=RuntimeRef(type="codex", version=manifest.runtime.version),
                instructions=Instructions(
                    system=manifest.prompt,
                    task=manifest.task_prompt or "",
                ),
                soul=manifest.soul,
                bindings=AgentBindings(
                    model_profile_id=default_profile,
                    model_profile_ids=profiles,
                    skills=skill_bindings,
                    mcp_servers=mcp_bindings,
                    plugins=list(manifest.plugins or []),
                ),
                context=context_spec,
                memory=memory_spec,
            ),
        )

    def _manifest(
        self,
        agent_id: str,
        spec: AgentSpec,
        *,
        current: CodexAgentManifest | None = None,
    ) -> CodexAgentManifest:
        model = self._model_name(spec, agent_id=agent_id)
        models = self._model_names(spec, default_model=model, agent_id=agent_id)
        prompt = spec.instructions.system.strip()
        task_prompt = spec.instructions.task.strip() or None
        skill_ids = self._skill_resource_ids(spec)
        mcp_servers = self._mcp_server_configs(spec)
        if current is not None:
            _current_bindings, unresolved_current = self._mcp_bindings(current)
            unresolved_names = {item["name"] for item in unresolved_current}
            mcp_servers.extend(
                dict(item)
                for item in (current.mcp_servers or [])
                if str(item.get("name") or "").strip() in unresolved_names
                and str(item.get("name") or "").strip()
                not in {str(server.get("name") or "").strip() for server in mcp_servers}
            )
        # PCM 策略写入 Manifest（随 Build 锁定，不可变）
        context_payload = (
            spec.context.model_dump(by_alias=True, exclude_none=True, mode="json") or None
        )
        memory_payload = (
            spec.memory.model_dump(by_alias=True, exclude_none=True, mode="json") or None
        )
        return CodexAgentManifest(
            name=agent_id,
            version=current.version if current is not None else "1.0.0",
            runtime=CodexRuntimeRef(version=self._runtime_version(spec, current=current)),
            model=model,
            models=models if len(models) > 1 else None,
            prompt=prompt,
            task_prompt=task_prompt,
            soul=spec.soul,
            skills=skill_ids or None,
            mcp_servers=mcp_servers or None,
            plugins=list(spec.bindings.plugins) or None,
            sandbox=spec.execution.sandbox,
            approval_mode=spec.execution.approval_mode,
            context=context_payload,
            memory=memory_payload,
        )

    @staticmethod
    def _runtime_version(
        spec: AgentSpec,
        *,
        current: CodexAgentManifest | None = None,
    ) -> str:
        if spec.runtime is not None and spec.runtime.version:
            return spec.runtime.version
        return (
            current.runtime.version
            if current is not None
            else (installed_runtime_version("codex") or "0.144.4")
        )

    def _model_name(self, spec: AgentSpec, *, agent_id: str | None = None) -> str:
        resolved = self.studio.catalog.resolve_model(spec.bindings)
        if resolved is not None:
            return str(resolved.model)
        if spec.model is not None:
            return spec.model.model
        if agent_id and self.studio.codex_manifests.exists(agent_id):
            return str(self.studio.codex_manifests.load(agent_id).manifest.model)
        configured = os.environ.get("OPENAI_MODEL_NAME", "").strip()
        return configured or "glm-5.1"

    def _model_names(
        self,
        spec: AgentSpec,
        *,
        default_model: str,
        agent_id: str | None,
    ) -> builtins.list[str]:
        names = [item.model for item in self.studio.catalog.resolve_models(spec.bindings)]
        if not names and agent_id and self.studio.codex_manifests.exists(agent_id):
            names = builtins.list(
                self.studio.codex_manifests.load(agent_id).manifest.allowed_models
            )
        if not names and spec.model is not None:
            names = [spec.model.model]
        names = [default_model, *[name for name in names if name != default_model]]
        return builtins.list(dict.fromkeys(names))

    def _model_bindings(
        self,
        manifest: CodexAgentManifest,
    ) -> tuple[str | None, builtins.list[str]]:
        resources: dict[str, str] = {}
        for descriptor in self.studio.catalog.list(kind="model", limit=500):
            try:
                model_spec = ModelSpec.model_validate(descriptor.contract)
            except ValueError:
                continue
            resources.setdefault(model_spec.model, descriptor.resource_id)
        default = resources.get(manifest.model)
        profiles = [resources[model] for model in manifest.allowed_models if model in resources]
        return default, profiles if default in profiles else []

    def _mcp_bindings(
        self,
        manifest: CodexAgentManifest,
    ) -> tuple[builtins.list[CapabilityBinding], builtins.list[dict[str, str]]]:
        """Project YAML MCP configs to real catalog bindings without inventing ids."""

        resources: dict[tuple[str, str], str] = {}
        for descriptor in self.studio.catalog.list(kind="mcp", limit=500):
            contract = descriptor.contract or {}
            name = str(contract.get("name") or descriptor.name or "").strip()
            url = str(
                contract.get("endpointUrl")
                or contract.get("endpoint_url")
                or contract.get("url")
                or ""
            ).strip()
            transport = str(contract.get("transport") or ("http" if url else "")).lower()
            address = (
                json.dumps(
                    [str(contract.get("command") or ""), *(contract.get("args") or [])],
                    separators=(",", ":"),
                )
                if transport == "stdio"
                else url
            )
            if name and address:
                resources.setdefault((name, address), descriptor.resource_id)

        bindings: builtins.list[CapabilityBinding] = []
        unresolved: builtins.list[dict[str, str]] = []
        for entry in manifest.mcp_servers or []:
            name = str(entry.get("name") or "").strip()
            transport = str(
                entry.get("transport") or ("http" if entry.get("url") else "")
            ).lower()
            address = (
                json.dumps(
                    [str(entry.get("command") or ""), *(entry.get("args") or [])],
                    separators=(",", ":"),
                )
                if transport == "stdio"
                else str(entry.get("url") or "").strip()
            )
            resource_id = resources.get((name, address))
            if resource_id:
                bindings.append(CapabilityBinding(resource_id=resource_id))
            else:
                unresolved.append({
                    "name": name or "未命名 MCP",
                    "reason": "not-in-resource-catalog",
                })
        return bindings, unresolved

    @staticmethod
    def _labels(manifest: CodexAgentManifest) -> dict[str, str]:
        return {
            "agentkit.ksyun.com/template": "blank",
            "agentkit.ksyun.com/framework": "codex",
            "agentkit.ksyun.com/artifact-type": "ManagedRuntime",
            "agentkit.ksyun.com/model": manifest.model,
            "agentkit.ksyun.com/models": ",".join(manifest.allowed_models),
            "agentkit.ksyun.com/runtime-version": manifest.runtime.version,
            "agentkit.ksyun.com/manifest-version": manifest.version,
        }

    def _builds(self, agent_id: str) -> builtins.list[CodexBuildRecord]:
        return [item for item in self.studio.codex_builds.list() if item.agent_name == agent_id]


__all__ = ["CodexAgentService", "CodexDraftRepository"]
