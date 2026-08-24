"""Codex Studio 的本地 ManagedRuntime 审计构建。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, cast

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from ksadk.builders.managed_runtime_builder import (
    ManagedRuntimeBuilder,
    managed_runtime_lock_path,
)
from ksadk.managed_runtime import (
    ManagedRuntimeError,
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

    def manifest_text(self, record: CodexBuildRecord) -> str:
        """Read the exact declaration retained by a successful local build.

        A ManagedRuntime rollback must use the target build's immutable
        declaration, rather than today's editable Agent YAML.  New builds
        retain canonical YAML plus a sibling lock, never a code ZIP or a KS3
        artifact.  Pre-existing two-file ZIP receipts remain readable so an
        upgrade does not make historical declaration rollbacks impossible.
        """

        artifact = self.workspace.resolve(record.artifact_path, must_exist=True)
        try:
            if artifact.suffix == ".zip":
                with zipfile.ZipFile(artifact) as archive:
                    if set(archive.namelist()) != {"agentengine.yaml", "runtime-lock.json"}:
                        raise ValueError("unexpected managed runtime bundle entries")
                    manifest = archive.read("agentengine.yaml")
                    lock = json.loads(archive.read("runtime-lock.json"))
            else:
                manifest = artifact.read_bytes()
                lock = json.loads(managed_runtime_lock_path(artifact).read_bytes())
        except (OSError, ValueError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
            raise StudioError(
                "CODEX_BUILD_ARTIFACT_INVALID",
                "Codex Build 的声明式运行时审计产物不可用",
                status_code=409,
                details={"id": record.id},
            ) from exc

        digest = hashlib.sha256(manifest).hexdigest()
        if digest != record.manifest_sha256 or str(lock.get("manifest_sha256") or "") != digest:
            raise StudioError(
                "CODEX_BUILD_DIGEST_MISMATCH",
                "Codex Build 审计产物与记录摘要不一致",
                status_code=409,
                details={
                    "id": record.id,
                    "expected": record.manifest_sha256,
                    "actual": digest,
                    "lock": str(lock.get("manifest_sha256") or ""),
                },
            )
        try:
            return manifest.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StudioError(
                "CODEX_BUILD_ARTIFACT_INVALID",
                "Codex Build 的声明不是 UTF-8 文本",
                status_code=409,
                details={"id": record.id},
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
            for receipt_file in self._receipt_files(artifact):
                self._remove_file(
                    receipt_file,
                    purge=purge,
                    destination=(
                        None
                        if trash_directory is None
                        else trash_directory / "artifacts" / receipt_file.name
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

    @staticmethod
    def _receipt_files(artifact: Path) -> tuple[Path, ...]:
        """Return declaration files for a new receipt or one legacy ZIP."""

        if artifact.suffix == ".zip":
            return (artifact,)
        return (artifact, managed_runtime_lock_path(artifact))


def normalize_proxy_mode(value: Any) -> Literal["forced", "auto", "direct"]:
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "forced"}:
        return "forced"
    if normalized in {"0", "direct"}:
        return "direct"
    return "auto"


def proxy_mode_env_value(value: Any) -> str | None:
    mode = normalize_proxy_mode(value)
    if mode == "forced":
        return "1"
    if mode == "direct":
        return "0"
    return None


def current_proxy_mode() -> Literal["forced", "auto", "direct"]:
    return normalize_proxy_mode(os.environ.get("KSADK_CODEX_USE_PROXY"))


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
            ignore_missing=True,
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
        try:
            sdk_version, installed_runtime, cli_version = self.runtime_inspector(runtime)
        except ManagedRuntimeError as exc:
            raise StudioError(
                "CODEX_RUNTIME_UNAVAILABLE",
                str(exc),
                status_code=422,
                details={"runtime": runtime.name, "expected": runtime.version},
            ) from exc
        if installed_runtime != runtime.version:
            raise StudioError(
                "CODEX_RUNTIME_VERSION_MISMATCH",
                "本地 Codex Runtime 与 agentengine.yaml 锁定版本不一致",
                status_code=422,
                details={"expected": runtime.version, "installed": installed_runtime},
            )

        result = ManagedRuntimeBuilder(
            self.workspace.root,
            # 使用仓储已经规范化并计算摘要的同一份 wire payload；不能再次从
            # Pydantic model_dump 生成，否则嵌套 ContractModel 的 alias 会改变字节。
            config=yaml.safe_load(snapshot.source_bytes),
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
        try:
            current_profiles = self._model_profile_snapshot(
                snapshot.manifest.name,
                allowed_models=snapshot.manifest.allowed_models,
            )
        except StudioError as exc:
            if exc.code == "RESOURCE_NOT_FOUND":
                # A completed Build owns its connection snapshot.  A later
                # Catalog cleanup must not make that immutable Build
                # undeployable; launch resolution reads the snapshot instead.
                return True
            raise
        return record.model_profiles == current_profiles

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
        ignore_missing: bool = False,
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
            try:
                descriptor = self.catalog.get(resource_id)
            except StudioError as exc:
                if exc.code == "RESOURCE_NOT_FOUND" and ignore_missing:
                    # Provider-discovered model profiles are process-local. A
                    # YAML-managed Agent may therefore retain a stale draft
                    # binding after Studio restarts even though its manifest
                    # still has a complete model declaration. In that case the
                    # runtime falls back to the configured model environment;
                    # the missing snapshot must not make a new Build impossible.
                    continue
                raise
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
        if artifact_path.suffix == ".zip":
            with zipfile.ZipFile(artifact_path) as archive:
                return cast(dict, json.loads(archive.read("runtime-lock.json")))
        return cast(dict, json.loads(managed_runtime_lock_path(artifact_path).read_bytes()))


__all__ = [
    "CodexBuildRecord",
    "CodexBuildRepository",
    "CodexStudioBuilder",
    "current_proxy_mode",
]
