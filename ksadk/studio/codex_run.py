"""Resolve an immutable Codex build into a canonical StudioRunSpec."""

from __future__ import annotations

import os
import zipfile
from typing import cast

import yaml  # type: ignore[import-untyped]

from ksadk.runtime import RuntimeLaunchContext
from ksadk.studio.codex_builder import CodexBuildRepository
from ksadk.studio.codex_manifest import CodexAgentManifest, CodexManifestRepository
from ksadk.studio.errors import StudioError
from ksadk.studio.run_service import StudioRunSpec
from ksadk.studio.workspace import Workspace


class CodexRunSpecResolver:
    """Validate a build snapshot without creating another Runtime abstraction."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        build_repository: CodexBuildRepository | None = None,
        manifest_repository: CodexManifestRepository | None = None,
    ) -> None:
        self.workspace = workspace
        self.builds = build_repository or CodexBuildRepository(workspace)
        self.manifests = manifest_repository or CodexManifestRepository(workspace)

    def resolve(self, build_id: str, *, model: str | None = None) -> StudioRunSpec:
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
        return StudioRunSpec(
            launch_context=RuntimeLaunchContext(
                runtime_type="codex",
                project_dir=project_dir,
                config={"sandbox_read_only": True},
            ),
            build_id=build.id,
            agent_id=manifest.name,
            model=selected_model,
            request_config={
                "base_instructions": manifest.prompt,
                "cwd": str(project_dir),
            },
            manifest_sha256=build.manifest_sha256,
        )

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
            with zipfile.ZipFile(archive_path) as archive:
                payload = yaml.safe_load(archive.read("agentengine.yaml"))
            return cast(CodexAgentManifest, CodexAgentManifest.model_validate(payload))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
            raise StudioError(
                "CODEX_BUILD_INVALID",
                "Codex Build 缺少有效的 agentengine.yaml",
                status_code=500,
            ) from exc


__all__ = ["CodexRunSpecResolver"]
