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
from ksadk.plugins.contracts import (
    LockedPluginComponent,
    PluginLock,
    PluginLockEntry,
    plugin_lock_digest,
)
from ksadk.studio.codex_manifest import CodexManifestRepository
from ksadk.studio.codex_plugin_store import (
    CodexPinnedMarketplace,
    CodexPluginSnapshotStore,
    CodexWorkspacePluginSnapshot,
    component_selector,
)
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
    # Resource ids and model names are different namespaces.  Keep the exact
    # Studio bindings separately so currentness checks never compare a model
    # name (``glm-5.2``) with a Catalog id (``model:provider:glm-5-2:live``).
    # ``None`` only allows older local records to be read and rejected as
    # stale with an actionable rebuild; Phase 2 has not shipped yet, so the
    # deployment path does not carry a legacy identity-migration branch.
    model_profile_ids: list[str] | None = None
    # Omitted for builds without native plugins so existing on-disk receipts
    # and their historical build ids remain byte-shape compatible.
    plugin_lock: PluginLock | None = None
    plugin_lock_digest: str | None = None
    plugin_marketplace: CodexPinnedMarketplace | None = None
    plugin_runtime_status: dict[str, dict[str, Any]] | None = None
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
        plugin_snapshot_store: CodexPluginSnapshotStore | None = None,
    ) -> None:
        self.workspace = workspace
        self.manifests = manifest_repository or CodexManifestRepository(workspace)
        self.repository = build_repository or CodexBuildRepository(workspace)
        self.runtime_inspector = runtime_inspector
        self.catalog = resource_catalog
        self.drafts = draft_repository
        self.plugin_snapshots = plugin_snapshot_store or CodexPluginSnapshotStore(workspace)

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
        model_profile_ids = self._bound_model_profile_ids(snapshot.manifest.name)
        (
            native_plugin_lock,
            plugin_selections,
            plugin_runtime_status,
        ) = self._native_plugin_lock(snapshot.manifest.plugins or [])
        native_plugin_lock_digest = (
            plugin_lock_digest(native_plugin_lock) if native_plugin_lock.plugins else None
        )
        build_id = self._build_id(
            snapshot.manifest_sha256,
            model_profiles,
            model_profile_ids=model_profile_ids,
            plugin_lock_digest_value=native_plugin_lock_digest,
        )
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
        pinned_marketplace = (
            self.plugin_snapshots.materialize_marketplace(
                plugin_lock_digest=native_plugin_lock_digest,
                selections=plugin_selections,
            )
            if native_plugin_lock_digest is not None
            else None
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
            model_profile_ids=model_profile_ids,
            plugin_lock=native_plugin_lock if native_plugin_lock.plugins else None,
            plugin_lock_digest=native_plugin_lock_digest,
            plugin_marketplace=pinned_marketplace,
            plugin_runtime_status=plugin_runtime_status or None,
            created_at=datetime.now(timezone.utc),
        )
        return self.repository.save(record)

    def is_current(self, record: CodexBuildRecord) -> bool:
        snapshot = self.manifests.load(record.agent_name)
        if record.manifest_sha256 != snapshot.manifest_sha256:
            return False
        current_plugin_lock, _selections, _status = self._native_plugin_lock(
            snapshot.manifest.plugins or []
        )
        current_plugin_digest = (
            plugin_lock_digest(current_plugin_lock) if current_plugin_lock.plugins else None
        )
        if record.plugin_lock_digest != current_plugin_digest:
            return False
        if record.plugin_lock is not None and record.plugin_lock != current_plugin_lock:
            return False
        if record.plugin_lock is None and current_plugin_lock.plugins:
            return False
        if record.plugin_marketplace is not None:
            try:
                self.plugin_snapshots.materialize_marketplace(
                    plugin_lock_digest=record.plugin_marketplace.plugin_lock_digest,
                    selections=_selections,
                )
            except (StudioError, ValueError):
                return False
        if record.model_profiles is None:
            return True
        if record.model_profile_ids is None:
            return False
        draft = self.drafts.get(record.agent_name) if self.drafts is not None else None
        if draft is None:
            return not record.model_profiles

        bound_ids = set(self._bound_model_profile_ids(record.agent_name))
        if set(record.model_profile_ids) != bound_ids:
            return False
        current_profiles = self._model_profile_snapshot(
            snapshot.manifest.name,
            allowed_models=snapshot.manifest.allowed_models,
            ignore_missing=True,
        )
        # Provider-discovered Catalog entries are process-local.  A restart may
        # temporarily remove them, but the Build still owns an immutable,
        # runnable connection snapshot and must remain deployable.
        if bound_ids and not current_profiles:
            return True
        return record.model_profiles == current_profiles

    @staticmethod
    def _build_id(
        manifest_sha256: str,
        model_profiles: dict[str, dict[str, Any]],
        *,
        model_profile_ids: list[str] | None = None,
        plugin_lock_digest_value: str | None = None,
    ) -> str:
        if not model_profiles and not model_profile_ids and plugin_lock_digest_value is None:
            return f"build_{manifest_sha256[:20]}"
        fingerprint = json.dumps(
            {
                "profiles": model_profiles,
                "resourceIds": sorted(model_profile_ids or []),
                "pluginLockDigest": plugin_lock_digest_value,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        import hashlib

        digest = hashlib.sha256(f"{manifest_sha256}\n{fingerprint}".encode()).hexdigest()
        return f"build_{digest[:20]}"

    def _native_plugin_lock(
        self,
        bindings: list[Any] | tuple[Any, ...],
    ) -> tuple[
        PluginLock,
        tuple[
            tuple[CodexWorkspacePluginSnapshot, tuple[Any, ...]],
            ...,
        ],
        dict[str, dict[str, Any]],
    ]:
        entries: list[PluginLockEntry] = []
        selections: list[
            tuple[CodexWorkspacePluginSnapshot, tuple[Any, ...]]
        ] = []
        statuses: dict[str, dict[str, Any]] = {}
        for binding in bindings:
            if not binding.enabled:
                continue
            if binding.ecosystem != "codex":
                raise StudioError(
                    "CODEX_PLUGIN_ECOSYSTEM_UNSUPPORTED",
                    "Codex Agent 目前只能运行 Codex 原生插件绑定",
                    status_code=422,
                    details={"pluginRef": binding.plugin_ref},
                )
            stored = self.plugin_snapshots.load(binding.snapshot_digest)
            if stored.plugin_ref != binding.plugin_ref:
                raise StudioError(
                    "CODEX_PLUGIN_BINDING_MISMATCH",
                    "Codex 插件绑定与不可变快照身份不一致",
                    status_code=409,
                    details={
                        "pluginRef": binding.plugin_ref,
                        "snapshotPluginRef": stored.plugin_ref,
                        "snapshotDigest": binding.snapshot_digest,
                    },
                )
            selected = stored.select_components(binding.components)
            plugin_id, version = binding.plugin_ref.removeprefix("plugin://").rsplit("@", 1)
            components = tuple(
                LockedPluginComponent(
                    id=component_selector(component),
                    kind=component.kind,
                    digest=component.content_digest,
                    path=component.path,
                )
                for component in selected
            )
            entries.append(
                PluginLockEntry(
                    id=plugin_id,
                    version=version,
                    digest=stored.artifact_digest,
                    source="local" if stored.source.type == "local" else "market",
                    license=stored.manifest.license,
                    upstream=stored.source.to_plugin_source_snapshot(),
                    components=components,
                )
            )
            hook_selectors = [
                component_selector(component)
                for component in selected
                if component.kind == "hook"
            ]
            statuses[binding.plugin_ref] = (
                {
                    "runnable": False,
                    "hookTrust": "unsupported",
                    "reason": "official-trust-api-unavailable",
                    "components": hook_selectors,
                }
                if hook_selectors
                else {"runnable": True, "hookTrust": "not-required"}
            )
            selections.append((stored, selected))
        try:
            lock = PluginLock(plugins=entries)
        except ValueError as exc:
            raise StudioError(
                "CODEX_PLUGIN_LOCK_INVALID",
                "Codex 插件绑定无法编译为唯一、精确的 PluginLock",
                status_code=422,
                details={"reason": str(exc)},
            ) from exc
        return lock, tuple(selections), statuses

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
                exclude={"metadata", "discovery"},
                mode="json",
            )
        return profiles

    def _bound_model_profile_ids(self, agent_id: str) -> list[str]:
        if self.drafts is None:
            return []
        draft = self.drafts.get(agent_id)
        if draft is None:
            return []
        bindings = draft.spec.bindings
        resource_ids = list(getattr(bindings, "model_profile_ids", []) or [])
        default_id = getattr(bindings, "model_profile_id", None)
        if not resource_ids and default_id:
            resource_ids = [default_id]
        return list(dict.fromkeys(resource_ids))

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
