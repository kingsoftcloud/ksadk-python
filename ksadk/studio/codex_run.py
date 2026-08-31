"""Resolve an immutable Codex build into a canonical StudioRunSpec."""

from __future__ import annotations

import os
import zipfile
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

from ksadk.configs import ModelConfig
from ksadk.runtime import RuntimeLaunchContext
from ksadk.studio.codex_builder import CodexBuildRepository
from ksadk.studio.codex_manifest import CodexAgentManifest, CodexManifestRepository
from ksadk.studio.contracts import Instructions, ModelSpec
from ksadk.studio.errors import StudioError
from ksadk.studio.run_service import StudioRunSpec
from ksadk.studio.soul import compose_system_instruction
from ksadk.studio.workspace import Workspace
from ksadk.tools.gateway import normalize_tool_approval_mode


class CodexRunSpecResolver:
    """Validate a build snapshot without creating another Runtime abstraction."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        build_repository: CodexBuildRepository | None = None,
        manifest_repository: CodexManifestRepository | None = None,
        credential_resolver: Any = None,
        resource_catalog: Any = None,
        draft_repository: Any = None,
    ) -> None:
        self.workspace = workspace
        self.builds = build_repository or CodexBuildRepository(workspace)
        self.manifests = manifest_repository or CodexManifestRepository(workspace)
        self.credentials = credential_resolver
        self.catalog = resource_catalog
        self.drafts = draft_repository

    def resolve(
        self,
        build_id: str,
        *,
        model: str | None = None,
        sandbox: str | None = None,
        approval_mode: str | None = None,
    ) -> StudioRunSpec:
        build = self.builds.get(build_id)
        current = self.manifests.load(build.agent_name)
        if build.manifest_sha256 != current.manifest_sha256:
            raise StudioError(
                "CODEX_BUILD_STALE",
                "agentengine.yaml 已修改，请重新构建后再运行",
                status_code=409,
                details={
                    "buildManifestSha256": build.manifest_sha256,
                    "currentManifestSha256": current.manifest_sha256,
                },
            )
        manifest = self._load_build_manifest(build.artifact_path)
        selected_model = self._select_model(manifest, model)
        project_dir = self.workspace.root.resolve()
        skills = self._skill_inputs(manifest)
        codex_overrides = self._mcp_overrides(manifest)
        approval_profile = normalize_tool_approval_mode(approval_mode) if approval_mode else ""
        sandbox, approval = self._resolve_sandbox(
            manifest,
            override=sandbox,
            approval_mode=approval_profile or None,
        )
        if sandbox == "workspace-write":
            # workspace-write 默认断网；MCP/搜索类工具需要显式放行网络
            codex_overrides = [*codex_overrides, "sandbox_workspace_write.network_access=true"]
        launch_config: dict[str, Any] = {
            "sandbox_read_only": sandbox == "read-only",
            "sandbox": sandbox,
            "approval_mode": approval,
        }
        if codex_overrides:
            launch_config["codex_overrides"] = codex_overrides
        runtime_env = {
            **self._resolve_model_env(build, manifest, selected_model),
            **self._resolve_mcp_env(manifest),
        }
        if runtime_env:
            launch_config["env"] = runtime_env
        agent_task = str(manifest.task_prompt or "").strip()
        agent_system = compose_system_instruction(
            Instructions(system=manifest.prompt),
            manifest.soul,
        ).system
        # PCM 策略从不可变 Manifest 读取（方案 §5.1：Build 锁定后 sidecar 修改不影响旧 Build）
        # manifest.context/memory 由 _manifest() 从 AgentSpec 写入，随 Build 进入 Artifact
        resolved_context = manifest.context
        resolved_memory = manifest.memory
        base_instructions = agent_system
        if agent_task:
            base_instructions = f"{agent_system}\n\n{agent_task}"
        request_config: dict[str, Any] = {
            # Codex 原生只接收 base_instructions，因此运行前合并；PCM 证据仍使用下面
            # 两个独立来源生成 agent_identity / agent_policy 的分段 hash。
            "base_instructions": base_instructions,
            "agent_system": agent_system,
            "agent_task": agent_task,
            "cwd": str(project_dir),
            "skills": skills,
            "sandbox_read_only": sandbox == "read-only",
            "sandbox": sandbox,
            "approval_mode": approval,
            "summary": "auto",
            # Studio sessions resume the same native Codex thread across turns.
            "ephemeral": False,
            # PCM 配置（方案 §5.1）：从 Manifest 读取预算和 rollout
            "max_input_tokens": resolved_context.max_input_tokens if resolved_context else None,
            "reserve_output_tokens": (
                resolved_context.reserve_output_tokens if resolved_context else None
            ),
            "context_engine_rollout": (
                resolved_context.rollout.context_engine if resolved_context else None
            ),
            "memory_recall_enabled": (resolved_memory.recall.enabled if resolved_memory else None),
            "memory_recall_top_k": resolved_memory.recall.top_k if resolved_memory else None,
            "memory_recall_max_tokens": (
                resolved_memory.recall.max_tokens if resolved_memory else None
            ),
            "memory_recall_min_score": (
                resolved_memory.recall.min_score if resolved_memory else None
            ),
            "memory_write_rollout": (
                resolved_context.rollout.memory_write if resolved_context else None
            ),
            "memory_enabled": resolved_memory.enabled if resolved_memory else False,
            "memory_write_mode": resolved_memory.write.mode if resolved_memory else "candidate",
            "flush_before_compaction": (
                resolved_memory.write.flush_before_compaction if resolved_memory else True
            ),
            "provider_ref": resolved_memory.provider_ref if resolved_memory else "local-default",
        }
        if approval_profile:
            request_config["tool_approval_mode"] = approval_profile
        if manifest.soul is not None:
            request_config.update(
                {
                    "soul_source": manifest.soul_source,
                    "soul_digest": manifest.soul_digest,
                }
            )
        return StudioRunSpec(
            launch_context=RuntimeLaunchContext(
                runtime_type="codex",
                project_dir=project_dir,
                config=launch_config,
            ),
            build_id=build.id,
            agent_id=manifest.name,
            model=selected_model,
            request_config=request_config,
            manifest_sha256=build.manifest_sha256,
        )

    def _resolve_model_env(
        self,
        build: Any,
        manifest: CodexAgentManifest,
        selected_model: str,
    ) -> dict[str, str]:
        resolver = self.credentials
        if resolver is None:
            from ksadk.studio.model_client import CredentialResolver

            resolver = CredentialResolver(self.workspace)
            self.credentials = resolver
        profile = self._resolve_model_profile(build, manifest, selected_model)
        credential_ref = profile.credential_ref if profile else "env://OPENAI_API_KEY"
        try:
            credential = resolver.resolve(credential_ref)
        except StudioError:
            return {}
        base_url = (
            self._model_base_url(profile)
            if profile is not None
            else ModelConfig().api_base.rstrip("/")
        )
        return {
            "OPENAI_API_KEY": credential,
            "OPENAI_BASE_URL": base_url,
            "OPENAI_API_BASE": base_url,
            "OPENAI_MODEL_NAME": selected_model,
        }

    def _resolve_model_profile(
        self,
        build: Any,
        manifest: CodexAgentManifest,
        selected_model: str,
    ) -> ModelSpec | None:
        # New build records own an immutable connection snapshot.  An empty
        # mapping explicitly means this build uses the process/workspace
        # default connection; only legacy records may consult live Catalog
        # state for backward compatibility.
        snapshots = getattr(build, "model_profiles", None)
        if snapshots is not None:
            payload = snapshots.get(selected_model)
            return ModelSpec.model_validate(payload) if payload is not None else None

        if self.catalog is None:
            from ksadk.studio.resource_catalog import LocalResourceCatalog

            self.catalog = LocalResourceCatalog(self.workspace)
        if self.drafts is None:
            from ksadk.studio.codex_agent_service import CodexDraftRepository

            self.drafts = CodexDraftRepository(self.workspace)

        draft = self.drafts.get(manifest.name)
        if draft is not None:
            bindings = draft.spec.bindings
            resource_ids = list(bindings.model_profile_ids)
            if not resource_ids and bindings.model_profile_id:
                resource_ids = [bindings.model_profile_id]
            for resource_id in resource_ids:
                try:
                    descriptor = self.catalog.get(resource_id)
                    profile = ModelSpec.model_validate(descriptor.contract)
                except (StudioError, ValueError):
                    continue
                if profile.model == selected_model:
                    return profile

        matches: list[ModelSpec] = []
        for descriptor in self.catalog.list(kind="model", limit=500):
            try:
                profile = ModelSpec.model_validate(descriptor.contract)
            except ValueError:
                continue
            if profile.model == selected_model:
                matches.append(profile)
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _model_base_url(profile: ModelSpec) -> str:
        raw = str(profile.base_url or profile.endpoint_url or "").rstrip("/")
        for suffix in ("/chat/completions", "/responses"):
            if raw.endswith(suffix):
                return raw[: -len(suffix)]
        return raw

    @staticmethod
    def _mcp_overrides(manifest: CodexAgentManifest) -> list[str]:
        """Translate bound MCP servers into codex --config overrides."""
        overrides: list[str] = []
        for server in manifest.mcp_servers or []:
            name = str(server.get("name") or "").strip()
            url = str(server.get("url") or "").strip()
            if not name or not url:
                continue
            overrides.append(f"mcp_servers.{name}.url={url}")
            env_key = str(server.get("env_key") or "").strip()
            if env_key:
                overrides.append(f"mcp_servers.{name}.bearer_token_env_var={env_key}")
        return overrides

    def _resolve_mcp_env(self, manifest: CodexAgentManifest) -> dict[str, str]:
        """把 manifest 中 MCP 的 env_key 解析为真实值，注入 codex 子进程环境。

        codex app-server 的 bearer_token_env_var 直接读子进程环境变量，不经过
        Studio 的凭证解析链（session → 工作区 secrets.env → 环境变量），必须在
        启动前完成解析。缺凭证时快速失败并指出具体引用。
        """
        env: dict[str, str] = {}
        for server in manifest.mcp_servers or []:
            env_key = str(server.get("env_key") or "").strip()
            if not env_key:
                continue
            if env_key in env:
                continue
            resolver = self.credentials
            if resolver is None:
                from ksadk.studio.model_client import CredentialResolver

                resolver = CredentialResolver(self.workspace)
                self.credentials = resolver
            try:
                env[env_key] = resolver.resolve(f"env://{env_key}")
            except Exception as exc:
                raise StudioError(
                    "MCP_CREDENTIAL_MISSING",
                    f"MCP Server「{server.get('name')}」需要凭证 {env_key}，"
                    "请先在 MCP 页连接时保存该环境变量的值",
                    status_code=422,
                    details={
                        "server": str(server.get("name") or ""),
                        "reference": f"env://{env_key}",
                    },
                ) from exc
        return env

    @staticmethod
    def _resolve_sandbox(
        manifest: CodexAgentManifest,
        override: str | None = None,
        approval_mode: str | None = None,
    ) -> tuple[str, str]:
        """Resolve (sandbox, approval_mode): per-run override > manifest > env > default.

        UI 4 档: read_only / workspace_write / workspace_write_auto / full_access
        """
        presets = {
            "read_only": ("read-only", "deny_all"),
            "read-only": ("read-only", "deny_all"),
            "workspace_write": ("workspace-write", "deny_all"),
            "workspace-write": ("workspace-write", "deny_all"),
            "workspace_write_auto": ("workspace-write", "auto_review"),
            "workspace-write-auto": ("workspace-write", "auto_review"),
            "full_access": ("full-access", "deny_all"),
            "full-access": ("full-access", "deny_all"),
        }
        approval_presets = {
            "ask": ("workspace-write", "manual"),
            "risk": ("workspace-write", "auto_review"),
            "full": ("full-access", "deny_all"),
        }
        if approval_mode:
            return approval_presets[normalize_tool_approval_mode(approval_mode)]
        raw = (
            (override or os.environ.get("KSADK_CODEX_SANDBOX") or manifest.sandbox or "read_only")
            .strip()
            .lower()
        )
        return presets.get(raw, ("read-only", "deny_all"))

    def _skill_inputs(self, manifest: CodexAgentManifest) -> list[dict[str, str]]:
        """Resolve bound skill resource ids to codex SkillInput wire dicts."""
        if not manifest.skills:
            return []
        skills_root = self.workspace.resolve("capabilities/skills")
        inputs: list[dict[str, str]] = []
        for sid in manifest.skills:
            name = self._skill_name_from_id(sid)
            if not name:
                continue
            skill_dir = skills_root / name
            if not skill_dir.is_dir():
                continue
            inputs.append({"name": name, "path": str(skill_dir.resolve())})
        return inputs

    @staticmethod
    def _skill_name_from_id(resource_id: str) -> str:
        parts = str(resource_id).split(":")
        if len(parts) >= 4 and parts[0] == "skill":
            return parts[2]
        if len(parts) == 2:
            return parts[1]
        return ""

    @staticmethod
    def _select_model(manifest: CodexAgentManifest, requested: str | None) -> str:
        selected = str(requested or manifest.model).strip()
        if selected not in manifest.allowed_models:
            raise StudioError(
                "MODEL_NOT_BOUND",
                "请求模型未绑定到当前 Agent Build",
                status_code=422,
                details={
                    "model": selected,
                    "allowedModels": list(manifest.allowed_models),
                },
            )
        environment_allowlist = {
            item.strip()
            for item in os.environ.get("AGENTENGINE_MODEL_ALLOWLIST", "").split(",")
            if item.strip()
        }
        if environment_allowlist and selected not in environment_allowlist:
            raise StudioError(
                "MODEL_NOT_AVAILABLE",
                "请求模型不在当前运行环境的模型白名单中",
                status_code=422,
                details={"model": selected},
            )
        return selected

    def _load_build_manifest(self, artifact_path: str) -> CodexAgentManifest:
        archive_path = self.workspace.resolve(artifact_path, must_exist=True)
        try:
            if archive_path.suffix == ".zip":
                # Compatibility for historical local audit receipts.  New
                # YAML-only builds keep the declaration as a plain immutable
                # file, so they cannot be mistaken for a user-code package.
                with zipfile.ZipFile(archive_path) as archive:
                    payload = yaml.safe_load(archive.read("agentengine.yaml"))
            else:
                payload = yaml.safe_load(archive_path.read_bytes())
            return cast(CodexAgentManifest, CodexAgentManifest.model_validate(payload))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
            raise StudioError(
                "CODEX_BUILD_INVALID",
                "Codex Build 缺少有效的 agentengine.yaml",
                status_code=500,
            ) from exc


__all__ = ["CodexRunSpecResolver"]
