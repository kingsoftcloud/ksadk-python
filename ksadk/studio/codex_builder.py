"""Codex Studio 的本地 ManagedRuntime 审计构建。"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, cast

from pydantic import ValidationError

from ksadk.builders.managed_runtime_builder import ManagedRuntimeBuilder
from ksadk.managed_runtime import (
    ResolvedRuntime,
    validate_installed_runtime,
    validate_runtime_binary,
)
from ksadk.studio.codex_manifest import CodexManifestRepository
from ksadk.studio.contracts import ContractModel, ModelSpec
from ksadk.studio.errors import StudioError, not_found
from ksadk.studio.workspace import Workspace
from ksadk.version import VERSION as SDK_VERSION

RuntimeInspector = Callable[[ResolvedRuntime], tuple[str, str, str]]


class CodexBuildRecord(ContractModel):
    id: str
    status: Literal["SUCCEEDED"] = "SUCCEEDED"
    agent_name: str
    agent_version: str
    source_revision: int = 1
    artifact_path: str
    manifest_sha256: str
    runtime_name: Literal["codex"] = "codex"
    runtime_version: str
    sdk_version: str
    cli_version: str
    proxy_mode: Literal["forced", "auto", "direct"]
    runtime_lock: dict
    # ``None`` identifies legacy records that predate connection snapshots.
    # New builds always persist a mapping (possibly empty), so run resolution
    # never consults mutable Catalog state after the build has been created.
    model_profiles: dict[str, dict[str, Any]] | None = None
    created_at: datetime


class CodexBuildRepository:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def _path(self, build_id: str) -> Path:
        return self.workspace.resolve(Path(".agentkit/codex-builds") / f"{build_id}.json")

    def save(self, record: CodexBuildRecord) -> CodexBuildRecord:
        payload = record.model_dump(
            by_alias=True,
            exclude_none=True,
            mode="json",
        )
        self.workspace.atomic_write_text(
            self._path(record.id),
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return record

    def get(self, build_id: str) -> CodexBuildRecord:
        path = self._path(build_id)
        if not path.is_file():
            raise not_found("codex build", build_id)
        try:
            return cast(
                CodexBuildRecord,
                CodexBuildRecord.model_validate_json(path.read_text(encoding="utf-8")),
            )
        except (OSError, ValidationError) as exc:
            raise StudioError(
                "CODEX_BUILD_RECORD_INVALID",
                "Codex Build 记录损坏",
                status_code=500,
                details={"id": build_id},
            ) from exc

    def list(self) -> list[CodexBuildRecord]:
        directory = self.workspace.resolve(".agentkit/codex-builds")
        records: list[CodexBuildRecord] = []
        for path in directory.glob("build_*.json"):
            try:
                records.append(
                    CodexBuildRecord.model_validate_json(path.read_text(encoding="utf-8"))
                )
            except (OSError, ValidationError):
                continue
        return sorted(records, key=lambda item: item.created_at, reverse=True)

    def delete_for_agent(
        self,
        agent_id: str,
        *,
        purge: bool,
        trash_directory: Path | None = None,
    ) -> int:
        records = [item for item in self.list() if item.agent_name == agent_id]
        artifacts = {
            self.workspace.resolve(item.artifact_path) for item in records if item.artifact_path
        }
        for artifact in artifacts:
            self._remove_file(
                artifact,
                purge=purge,
                destination=(
                    None
                    if trash_directory is None
                    else trash_directory / "artifacts" / artifact.name
                ),
            )
        for record in records:
            path = self._path(record.id)
            self._remove_file(
                path,
                purge=purge,
                destination=(
                    None if trash_directory is None else trash_directory / "builds" / path.name
                ),
            )
        return len(records)

    def _remove_file(
        self,
        source: Path,
        *,
        purge: bool,
        destination: Path | None,
    ) -> None:
        if not source.is_file():
            return
        if purge:
            source.unlink()
            return
        if destination is None:
            raise ValueError("recoverable deletion requires a trash destination")
        target = self.workspace.resolve(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))


def current_proxy_mode() -> Literal["forced", "auto", "direct"]:
    override = os.environ.get("KSADK_CODEX_USE_PROXY")
    if override == "1":
        return "forced"
    if override == "0":
        return "direct"
    return "auto"


def _inspect_runtime(runtime: ResolvedRuntime) -> tuple[str, str, str]:
    installed = validate_installed_runtime(runtime)
    cli = validate_runtime_binary(runtime)
    return SDK_VERSION, installed, cli


class CodexStudioBuilder:
    def __init__(
        self,
        workspace: Workspace,
        *,
        manifest_repository: CodexManifestRepository | None = None,
        build_repository: CodexBuildRepository | None = None,
        runtime_inspector: RuntimeInspector = _inspect_runtime,
        resource_catalog: Any = None,
        draft_repository: Any = None,
    ) -> None:
        self.workspace = workspace
        self.manifests = manifest_repository or CodexManifestRepository(workspace)
        self.repository = build_repository or CodexBuildRepository(workspace)
        self.runtime_inspector = runtime_inspector
        self.catalog = resource_catalog
        self.drafts = draft_repository

    def build(
        self,
        agent_id: str | None = None,
        *,
        source_revision: int = 1,
    ) -> CodexBuildRecord:
        snapshot = self.manifests.load(agent_id)
        model_profiles = self._model_profile_snapshot(
            snapshot.manifest.name,
            allowed_models=snapshot.manifest.allowed_models,
        )
        build_id = self._build_id(snapshot.manifest_sha256, model_profiles)
        try:
            existing = self.repository.get(build_id)
        except StudioError as exc:
            if exc.status_code != 404:
                raise
        else:
            return existing

        runtime = ResolvedRuntime(
            name=snapshot.manifest.runtime.name,
            version=snapshot.manifest.runtime.version,
            source="manifest",
        )
        sdk_version, installed_runtime, cli_version = self.runtime_inspector(runtime)
        if installed_runtime != runtime.version:
            raise StudioError(
                "CODEX_RUNTIME_VERSION_MISMATCH",
                "本地 Codex Runtime 与 agentengine.yaml 锁定版本不一致",
                status_code=422,
                details={"expected": runtime.version, "installed": installed_runtime},
            )

        result = ManagedRuntimeBuilder(
            self.workspace.root,
            config=snapshot.manifest.model_dump(mode="python", exclude_none=True),
            runtime_version=runtime.version,
        ).build()
        if not result.success or result.artifact_path is None:
            raise StudioError(
                "CODEX_BUILD_FAILED",
                result.error_message or "Codex ManagedRuntime 构建失败",
                status_code=422,
            )
        lock = self._runtime_lock(result.artifact_path)
        lock_sha = str(lock.get("manifest_sha256") or "")
        if lock_sha != snapshot.manifest_sha256:
            raise StudioError(
                "CODEX_BUILD_DIGEST_MISMATCH",
                "构建产物与当前 agentengine.yaml 摘要不一致",
                status_code=500,
            )
        record = CodexBuildRecord(
            id=build_id,
            agent_name=snapshot.manifest.name,
            agent_version=snapshot.manifest.version,
            source_revision=source_revision,
            artifact_path=self.workspace.relative(result.artifact_path),
            manifest_sha256=snapshot.manifest_sha256,
            runtime_version=runtime.version,
            sdk_version=sdk_version,
            cli_version=cli_version,
            proxy_mode=current_proxy_mode(),
            runtime_lock=lock,
            model_profiles=model_profiles,
            created_at=datetime.now(timezone.utc),
        )
        return self.repository.save(record)

    def is_current(self, record: CodexBuildRecord) -> bool:
        snapshot = self.manifests.load(record.agent_name)
        if record.manifest_sha256 != snapshot.manifest_sha256:
            return False
        if record.model_profiles is None:
            return True
        return record.model_profiles == self._model_profile_snapshot(
            snapshot.manifest.name,
            allowed_models=snapshot.manifest.allowed_models,
        )

    @staticmethod
    def _build_id(
        manifest_sha256: str,
        model_profiles: dict[str, dict[str, Any]],
    ) -> str:
        if not model_profiles:
            return f"build_{manifest_sha256[:20]}"
        fingerprint = json.dumps(
            model_profiles,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        import hashlib

        digest = hashlib.sha256(f"{manifest_sha256}\n{fingerprint}".encode()).hexdigest()
        return f"build_{digest[:20]}"

    def _model_profile_snapshot(
        self,
        agent_id: str,
        *,
        allowed_models: tuple[str, ...],
    ) -> dict[str, dict[str, Any]]:
        if self.catalog is None or self.drafts is None:
            return {}
        draft = self.drafts.get(agent_id)
        if draft is None:
            return {}
        bindings = draft.spec.bindings
        resource_ids = list(getattr(bindings, "model_profile_ids", []) or [])
        default_id = getattr(bindings, "model_profile_id", None)
        if not resource_ids and default_id:
            resource_ids = [default_id]
        profiles: dict[str, dict[str, Any]] = {}
        for resource_id in resource_ids:
            descriptor = self.catalog.get(resource_id)
            profile = ModelSpec.model_validate(descriptor.contract)
            if profile.model not in allowed_models or profile.model in profiles:
                continue
            profiles[profile.model] = profile.model_dump(
                by_alias=True,
                exclude_defaults=True,
                exclude_none=True,
                mode="json",
            )
        return profiles

    @staticmethod
    def _runtime_lock(artifact_path: Path) -> dict:
        import zipfile

        with zipfile.ZipFile(artifact_path) as archive:
            return cast(dict, json.loads(archive.read("runtime-lock.json")))


__all__ = [
    "CodexBuildRecord",
    "CodexBuildRepository",
    "CodexStudioBuilder",
    "current_proxy_mode",
]
