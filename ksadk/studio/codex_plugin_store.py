"""Immutable workspace snapshots and selective Codex plugin marketplaces.

Codex App Server owns plugin installation.  Studio admits the installed bytes
into a content-addressed workspace store, then builds a small local
marketplace from those immutable bytes.  Neither operation writes Codex's
host-managed cache directly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Literal

from pydantic import Field, field_validator

from ksadk.plugins.codex_manifest import (
    CodexInstalledPluginSnapshot,
    CodexPluginComponentSnapshot,
    CodexPluginManifest,
    CodexPluginManifestError,
    CodexPluginSourceCoordinate,
    _load_installed_plugin,
    snapshot_installed_codex_plugin,
)
from ksadk.studio.contracts import ContractModel
from ksadk.studio.errors import StudioError
from ksadk.studio.workspace import Workspace

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PLUGIN_ID = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_SELECTOR = re.compile(r"^(skill|mcp|hook|app):(.+)$")
_CACHE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")


class CodexWorkspacePluginSnapshot(ContractModel):
    """Receipt for bytes copied into the workspace content-addressed store."""

    snapshot_format: Literal["codex.workspace-plugin-snapshot/v1"] = (
        "codex.workspace-plugin-snapshot/v1"
    )
    snapshot_digest: str
    plugin_ref: str = Field(min_length=12, max_length=256)
    content_path: str = Field(min_length=1, max_length=4096)
    source: CodexPluginSourceCoordinate
    manifest: CodexPluginManifest
    manifest_digest: str
    artifact_digest: str
    components: tuple[CodexPluginComponentSnapshot, ...] = ()

    @field_validator("snapshot_digest", "manifest_digest", "artifact_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("Codex snapshot digests must be lowercase sha256 values")
        return value

    @field_validator("content_path")
    @classmethod
    def validate_content_path(cls, value: str) -> str:
        if "\\" in value:
            raise ValueError("Codex snapshot contentPath must use POSIX separators")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("Codex snapshot contentPath must be workspace-relative")
        return value

    def select_components(
        self, selectors: Iterable[str]
    ) -> tuple[CodexPluginComponentSnapshot, ...]:
        requested = tuple(dict.fromkeys(str(item) for item in selectors))
        by_selector = {
            component_selector(component): component for component in self.components
        }
        missing = [selector for selector in requested if selector not in by_selector]
        if missing:
            raise StudioError(
                "CODEX_PLUGIN_COMPONENT_NOT_FOUND",
                "Codex 插件绑定引用了快照中不存在的组件",
                status_code=422,
                details={"pluginRef": self.plugin_ref, "components": missing},
            )
        return tuple(by_selector[selector] for selector in requested)


class CodexPinnedMarketplace(ContractModel):
    marketplace_format: Literal["codex.pinned-marketplace/v1"] = (
        "codex.pinned-marketplace/v1"
    )
    marketplace_name: str
    marketplace_path: str
    marketplace_digest: str
    plugin_names: tuple[str, ...]
    plugin_lock_digest: str

    @field_validator("marketplace_digest", "plugin_lock_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("Codex marketplace digests must be lowercase sha256 values")
        return value


def component_selector(component: CodexPluginComponentSnapshot) -> str:
    return f"{component.kind}:{component.name}"


def codex_plugin_ref(manifest: CodexPluginManifest) -> str:
    version = str(manifest.version or "")
    if not version:
        raise StudioError(
            "CODEX_PLUGIN_VERSION_REQUIRED",
            "Codex 插件必须声明精确版本后才能提交不可变快照",
            status_code=422,
            field="version",
        )
    candidate = str(manifest.id or "").strip().lower()
    if not candidate or _PLUGIN_ID.fullmatch(candidate) is None:
        candidate = f"codex.{manifest.name.lower()}"
    if len(candidate) > 128 or _PLUGIN_ID.fullmatch(candidate) is None:
        raise StudioError(
            "CODEX_PLUGIN_ID_INVALID",
            "Codex 插件无法映射为稳定的 plugin reference",
            status_code=422,
            details={"name": manifest.name, "id": manifest.id},
        )
    return f"plugin://{candidate}@{version}"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _snapshot_identity(snapshot: CodexInstalledPluginSnapshot) -> dict[str, Any]:
    return {
        "source": snapshot.source.model_dump(by_alias=True, exclude_none=True, mode="json"),
        "manifest": snapshot.manifest.model_dump(
            by_alias=True, exclude_none=True, mode="json"
        ),
        "manifestDigest": snapshot.manifest_digest,
        "artifactDigest": snapshot.artifact_digest,
        "components": [
            item.model_dump(by_alias=True, exclude_none=True, mode="json")
            for item in snapshot.components
        ],
    }


def _snapshot_digest(snapshot: CodexInstalledPluginSnapshot) -> str:
    return _sha256(_canonical_bytes(_snapshot_identity(snapshot)))


def _same_snapshot(
    expected: CodexInstalledPluginSnapshot,
    actual: CodexInstalledPluginSnapshot,
) -> bool:
    return _snapshot_identity(expected) == _snapshot_identity(actual)


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for candidate in sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix()):
        relative = candidate.relative_to(root).as_posix()
        if candidate.is_symlink():
            raise StudioError(
                "CODEX_PLUGIN_MARKETPLACE_INVALID",
                "固定 Codex marketplace 不能包含符号链接",
                status_code=409,
                details={"path": relative},
            )
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise StudioError(
                "CODEX_PLUGIN_MARKETPLACE_INVALID",
                "固定 Codex marketplace 只能包含常规文件",
                status_code=409,
                details={"path": relative},
            )
        raw = candidate.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{stat.S_IMODE(candidate.stat().st_mode) & 0o111:03o}".encode("ascii"))
        digest.update(b"\0")
        digest.update(str(len(raw)).encode("ascii"))
        digest.update(b"\0")
        digest.update(raw)
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


class CodexPluginSnapshotStore:
    """Content-addressed, copy-then-rehash store for installed Codex plugins."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.root = workspace.resolve(".agentkit/codex-plugin-snapshots")

    def lookup(
        self, snapshot: CodexInstalledPluginSnapshot
    ) -> CodexWorkspacePluginSnapshot | None:
        """Return an already-admitted snapshot without mutating the workspace.

        The installed bytes still need to be hashed to derive their content
        address.  Unlike :meth:`commit`, lookup never creates directories or
        copies host-owned plugin bytes.
        """

        digest = _snapshot_digest(snapshot)
        receipt = self.root / digest.removeprefix("sha256:") / "snapshot.json"
        if not receipt.is_file():
            return None
        return self.load(digest)

    def commit(
        self, snapshot: CodexInstalledPluginSnapshot
    ) -> CodexWorkspacePluginSnapshot:
        source_root = Path(snapshot.installed_root)
        try:
            observed = snapshot_installed_codex_plugin(source_root, source=snapshot.source)
        except CodexPluginManifestError as exc:
            raise StudioError(
                "CODEX_PLUGIN_SNAPSHOT_INVALID",
                "Codex 插件安装目录无法提交快照",
                status_code=422,
                details={"reason": str(exc)},
            ) from exc
        if not _same_snapshot(snapshot, observed):
            raise StudioError(
                "CODEX_PLUGIN_SOURCE_DRIFT",
                "Codex 插件在提交快照前已发生变化",
                status_code=409,
                details={
                    "expected": snapshot.artifact_digest,
                    "actual": observed.artifact_digest,
                },
            )

        digest = _snapshot_digest(observed)
        suffix = digest.removeprefix("sha256:")
        destination = self.workspace.resolve(self.root / suffix)
        if destination.exists():
            return self.load(digest)

        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".codex-plugin-", dir=self.root))
        try:
            content = staging / "content"
            # Preserve a racing symlink as a symlink.  The second parser/hash
            # rejects it instead of following it outside the admitted root.
            shutil.copytree(source_root, content, symlinks=True)
            try:
                copied = snapshot_installed_codex_plugin(content, source=observed.source)
            except CodexPluginManifestError as exc:
                raise StudioError(
                    "CODEX_PLUGIN_COPY_DRIFT",
                    "Codex 插件复制期间发生不安全或不一致的变化",
                    status_code=409,
                    details={"reason": str(exc)},
                ) from exc
            if not _same_snapshot(observed, copied):
                raise StudioError(
                    "CODEX_PLUGIN_COPY_DRIFT",
                    "Codex 插件复制后的二次摘要不一致",
                    status_code=409,
                    details={
                        "expected": observed.artifact_digest,
                        "actual": copied.artifact_digest,
                    },
                )

            content_path = (destination / "content").relative_to(self.workspace.root).as_posix()
            record = CodexWorkspacePluginSnapshot(
                snapshot_digest=digest,
                plugin_ref=codex_plugin_ref(copied.manifest),
                content_path=content_path,
                source=copied.source,
                manifest=copied.manifest,
                manifest_digest=copied.manifest_digest,
                artifact_digest=copied.artifact_digest,
                components=copied.components,
            )
            (staging / "snapshot.json").write_bytes(
                json.dumps(
                    record.model_dump(by_alias=True, exclude_none=True, mode="json"),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
            try:
                os.replace(staging, destination)
            except FileExistsError:
                # A concurrent identical commit won the content-addressed path.
                pass
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return self.load(digest)

    def load(self, digest: str) -> CodexWorkspacePluginSnapshot:
        if _DIGEST.fullmatch(digest) is None:
            raise StudioError(
                "CODEX_PLUGIN_SNAPSHOT_DIGEST_INVALID",
                "Codex 插件快照摘要格式无效",
                status_code=422,
            )
        suffix = digest.removeprefix("sha256:")
        directory = self.workspace.resolve(self.root / suffix)
        receipt_path = self.workspace.resolve(directory / "snapshot.json")
        if not receipt_path.is_file():
            raise StudioError(
                "CODEX_PLUGIN_SNAPSHOT_NOT_FOUND",
                "Codex 插件快照不存在",
                status_code=404,
                details={"snapshotDigest": digest},
            )
        try:
            record = CodexWorkspacePluginSnapshot.model_validate_json(
                receipt_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise StudioError(
                "CODEX_PLUGIN_SNAPSHOT_RECEIPT_INVALID",
                "Codex 插件快照记录损坏",
                status_code=409,
                details={"snapshotDigest": digest},
            ) from exc
        expected_content = (directory / "content").relative_to(self.workspace.root).as_posix()
        if record.snapshot_digest != digest or record.content_path != expected_content:
            raise StudioError(
                "CODEX_PLUGIN_SNAPSHOT_RECEIPT_INVALID",
                "Codex 插件快照记录与内容地址不一致",
                status_code=409,
                details={"snapshotDigest": digest},
            )
        content = self.workspace.resolve(record.content_path)
        try:
            observed = snapshot_installed_codex_plugin(content, source=record.source)
        except CodexPluginManifestError as exc:
            raise StudioError(
                "CODEX_PLUGIN_SNAPSHOT_DRIFT",
                "Codex 插件不可变快照已损坏或漂移",
                status_code=409,
                details={"snapshotDigest": digest, "reason": str(exc)},
            ) from exc
        expected = CodexInstalledPluginSnapshot(
            installed_root=str(content),
            source=record.source,
            manifest=record.manifest,
            manifest_digest=record.manifest_digest,
            artifact_digest=record.artifact_digest,
            components=record.components,
        )
        if not _same_snapshot(expected, observed) or _snapshot_digest(observed) != digest:
            raise StudioError(
                "CODEX_PLUGIN_SNAPSHOT_DRIFT",
                "Codex 插件不可变快照的内容摘要已漂移",
                status_code=409,
                details={
                    "snapshotDigest": digest,
                    "expectedArtifactDigest": record.artifact_digest,
                    "actualArtifactDigest": observed.artifact_digest,
                },
            )
        return record

    def content_root(self, snapshot: CodexWorkspacePluginSnapshot) -> Path:
        return self.workspace.resolve(snapshot.content_path, must_exist=True)

    def verify_marketplace(self, receipt: CodexPinnedMarketplace) -> Path:
        root = self.workspace.resolve(receipt.marketplace_path, must_exist=True)
        actual = _tree_digest(root)
        if actual != receipt.marketplace_digest:
            raise StudioError(
                "CODEX_PLUGIN_MARKETPLACE_DRIFT",
                "固定 Codex marketplace 已发生漂移",
                status_code=409,
                details={
                    "pluginLockDigest": receipt.plugin_lock_digest,
                    "expected": receipt.marketplace_digest,
                    "actual": actual,
                },
            )
        return root

    def materialize_marketplace(
        self,
        *,
        plugin_lock_digest: str,
        selections: Iterable[
            tuple[CodexWorkspacePluginSnapshot, tuple[CodexPluginComponentSnapshot, ...]]
        ],
    ) -> CodexPinnedMarketplace:
        """Build a deterministic local marketplace with selected declarations only."""

        if _DIGEST.fullmatch(plugin_lock_digest) is None:
            raise ValueError("plugin_lock_digest must be a lowercase sha256 digest")
        selected_plugins = tuple(selections)
        suffix = plugin_lock_digest.removeprefix("sha256:")
        directory = self.workspace.resolve(
            Path(".agentkit/codex-plugin-marketplaces") / suffix
        )
        receipt_path = directory / "receipt.json"
        if receipt_path.is_file():
            receipt = CodexPinnedMarketplace.model_validate_json(
                receipt_path.read_text(encoding="utf-8")
            )
            root = self.workspace.resolve(receipt.marketplace_path, must_exist=True)
            if (
                receipt.plugin_lock_digest != plugin_lock_digest
                or _tree_digest(root) != receipt.marketplace_digest
            ):
                raise StudioError(
                    "CODEX_PLUGIN_MARKETPLACE_DRIFT",
                    "固定 Codex marketplace 已发生漂移",
                    status_code=409,
                    details={"pluginLockDigest": plugin_lock_digest},
                )
            return receipt

        directory.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".codex-marketplace-", dir=directory.parent))
        try:
            root = staging / "marketplace"
            plugins_root = root / "plugins"
            plugins_root.mkdir(parents=True)
            plugin_entries: list[dict[str, Any]] = []
            plugin_names: list[str] = []
            ordered_plugins = sorted(
                selected_plugins,
                key=lambda item: (item[0].manifest.name, item[0].plugin_ref),
            )
            names = [item[0].manifest.name for item in ordered_plugins]
            if len(names) != len(set(names)):
                raise StudioError(
                    "CODEX_PLUGIN_MARKETPLACE_NAME_CONFLICT",
                    "多个 Codex 插件快照映射到相同的原生插件名称",
                    status_code=422,
                    details={"pluginNames": names},
                )
            for snapshot, components in ordered_plugins:
                # Revalidate the immutable source immediately before copying.
                snapshot = self.load(snapshot.snapshot_digest)
                name = snapshot.manifest.name
                target = plugins_root / name
                shutil.copytree(self.content_root(snapshot), target, symlinks=True)
                self._retain_selected_components(target, components)
                plugin_names.append(name)
                plugin_entries.append(
                    {
                        "name": name,
                        "source": {"source": "local", "path": f"./plugins/{name}"},
                        "policy": {
                            "installation": "AVAILABLE",
                            "authentication": "ON_INSTALL",
                        },
                        "category": (
                            snapshot.manifest.interface.category
                            if snapshot.manifest.interface
                            and snapshot.manifest.interface.category
                            else "Productivity"
                        ),
                    }
                )
            marketplace_name = f"ksadk-{suffix[:20]}"
            marketplace = {
                "name": marketplace_name,
                "interface": {"displayName": "KsADK pinned plugins"},
                "plugins": plugin_entries,
            }
            marketplace_path = root / ".agents" / "plugins" / "marketplace.json"
            marketplace_path.parent.mkdir(parents=True)
            marketplace_path.write_bytes(
                json.dumps(marketplace, ensure_ascii=False, indent=2, sort_keys=True).encode(
                    "utf-8"
                )
                + b"\n"
            )
            digest = _tree_digest(root)
            final_root = directory / "marketplace"
            receipt = CodexPinnedMarketplace(
                marketplace_name=marketplace_name,
                marketplace_path=final_root.relative_to(self.workspace.root).as_posix(),
                marketplace_digest=digest,
                plugin_names=tuple(plugin_names),
                plugin_lock_digest=plugin_lock_digest,
            )
            (staging / "receipt.json").write_bytes(
                json.dumps(
                    receipt.model_dump(by_alias=True, mode="json"),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
            try:
                os.replace(staging, directory)
            except FileExistsError:
                pass
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return self.materialize_marketplace(
            plugin_lock_digest=plugin_lock_digest,
            selections=selected_plugins,
        )

    @staticmethod
    def _retain_selected_components(
        root: Path,
        selected: tuple[CodexPluginComponentSnapshot, ...],
    ) -> None:
        selected_keys = {(component.kind, component.name) for component in selected}
        (
            _validated_root,
            manifest,
            _raw,
            skills,
            mcp_servers,
            hooks,
            apps,
        ) = _load_installed_plugin(root)

        # The parser deliberately preserves additive upstream fields so the
        # inventory/read path remains forward compatible.  Selective
        # materialization is an execution boundary, however: an unknown
        # top-level field could name a new executable component that this
        # implementation does not know how to remove.  Fail closed until the
        # field has explicit selection semantics.
        if manifest.model_extra:
            raise StudioError(
                "CODEX_PLUGIN_MANIFEST_EXTENSION_UNSUPPORTED",
                "Codex 插件包含尚未支持的可执行清单字段，无法安全裁剪组件",
                status_code=422,
                details={"fields": sorted(manifest.model_extra)},
            )

        # Copy selected skill trees aside, remove every discovered skill tree,
        # then publish only the selected ones under the canonical skills root.
        skill_staging = Path(tempfile.mkdtemp(prefix=".selected-skills-", dir=root))
        try:
            for name, source in skills:
                if ("skill", name) in selected_keys:
                    shutil.copytree(source, skill_staging / name)
            for _name, source in sorted(skills, key=lambda item: len(item[1].parts), reverse=True):
                if source.exists():
                    shutil.rmtree(source)
            canonical_skills = root / "skills"
            if canonical_skills.exists():
                shutil.rmtree(canonical_skills)
            if any(kind == "skill" for kind, _name in selected_keys):
                canonical_skills.mkdir(parents=True)
                for source in sorted(skill_staging.iterdir(), key=lambda item: item.name):
                    shutil.copytree(source, canonical_skills / source.name)
        finally:
            shutil.rmtree(skill_staging, ignore_errors=True)

        # Remove all source declaration files before writing canonical selected
        # companions. Inline MCP declarations are replaced in plugin.json.
        companion_paths = {
            relative
            for _name, _config, relative in (*mcp_servers, *hooks, *apps)
            if relative
        }
        if isinstance(manifest.mcp_servers, str):
            companion_paths.add(manifest.mcp_servers)
        if manifest.apps:
            companion_paths.add(manifest.apps)
        if isinstance(manifest.hooks, str):
            companion_paths.add(manifest.hooks)
        elif isinstance(manifest.hooks, tuple):
            companion_paths.update(
                item for item in manifest.hooks if isinstance(item, str)
            )
        elif manifest.hooks is None and (root / "hooks" / "hooks.json").is_file():
            companion_paths.add("./hooks/hooks.json")
        for relative in companion_paths:
            candidate = root / PurePosixPath(relative)
            if candidate.is_file():
                candidate.unlink()
        selected_mcp = {
            name: config
            for name, config, _path in mcp_servers
            if ("mcp", name) in selected_keys
        }
        selected_hooks = {
            name: config["hooks"]
            for name, config, _path in hooks
            if ("hook", name) in selected_keys
        }
        selected_apps = {
            name: config
            for name, config, _path in apps
            if ("app", name) in selected_keys
        }
        if selected_mcp:
            (root / ".mcp.json").write_bytes(
                _canonical_bytes({"mcpServers": selected_mcp}) + b"\n"
            )
        if selected_hooks:
            (root / "hooks.json").write_bytes(
                _canonical_bytes({"hooks": selected_hooks}) + b"\n"
            )
        if selected_apps:
            (root / ".app.json").write_bytes(
                _canonical_bytes({"apps": selected_apps}) + b"\n"
            )

        payload = manifest.model_dump(by_alias=True, exclude_none=True, mode="json")
        for key in ("skills", "mcpServers", "mcp_servers", "hooks", "apps"):
            payload.pop(key, None)
        if any(kind == "skill" for kind, _name in selected_keys):
            payload["skills"] = "./skills/"
        if selected_mcp:
            payload["mcpServers"] = "./.mcp.json"
        if selected_hooks:
            payload["hooks"] = "./hooks.json"
        if selected_apps:
            payload["apps"] = "./.app.json"
        manifest_path = root / ".codex-plugin" / "plugin.json"
        manifest_path.write_bytes(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
            + b"\n"
        )
        # The same strict parser used at admission must accept generated bytes.
        _load_installed_plugin(root)


def find_installed_codex_plugin_root(
    codex_home: Path,
    *,
    marketplace_name: str,
    plugin_name: str,
    version: str | None,
) -> Path:
    """Locate the App Server-owned cache root without assuming it is writable."""

    coordinates = {
        "marketplaceName": marketplace_name,
        "pluginName": plugin_name,
        "version": version,
    }
    invalid = [
        field
        for field, value in coordinates.items()
        if value is not None and _CACHE_SEGMENT.fullmatch(value) is None
    ]
    if invalid:
        raise StudioError(
            "CODEX_PLUGIN_CACHE_COORDINATE_INVALID",
            "Codex 宿主返回的插件缓存坐标不是安全的单一路径段",
            status_code=409,
            details={"fields": invalid, **coordinates},
        )

    home = Path(codex_home).expanduser().resolve()
    cache_path = home / "plugins" / "cache"
    if any(path.is_symlink() for path in (home / "plugins", cache_path)):
        raise StudioError(
            "CODEX_PLUGIN_INSTALLED_ROOT_UNSAFE",
            "Codex 插件缓存路径不能经过符号链接",
            status_code=409,
            details={"cache": str(cache_path)},
        )
    try:
        cache = cache_path.resolve(strict=True) if cache_path.exists() else cache_path.resolve()
    except OSError as exc:
        raise StudioError(
            "CODEX_PLUGIN_INSTALLED_ROOT_UNSAFE",
            "Codex 插件缓存路径无法安全解析",
            status_code=409,
            details={"cache": str(cache_path)},
        ) from exc
    try:
        cache.relative_to(home)
    except ValueError as exc:  # defensive if platform resolution semantics change
        raise StudioError(
            "CODEX_PLUGIN_INSTALLED_ROOT_UNSAFE",
            "Codex 插件缓存路径越出 CODEX_HOME",
            status_code=409,
            details={"cache": str(cache)},
        ) from exc

    def safe_root(candidate: Path) -> Path:
        try:
            relative = candidate.relative_to(cache_path)
        except ValueError as exc:
            raise StudioError(
                "CODEX_PLUGIN_INSTALLED_ROOT_UNSAFE",
                "Codex 插件安装目录不在宿主缓存内",
                status_code=409,
                details={"path": str(candidate)},
            ) from exc
        cursor = cache_path
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                raise StudioError(
                    "CODEX_PLUGIN_INSTALLED_ROOT_UNSAFE",
                    "Codex 插件安装目录不能经过符号链接",
                    status_code=409,
                    details={"path": str(candidate)},
                )
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(cache)
        except (OSError, ValueError) as exc:
            raise StudioError(
                "CODEX_PLUGIN_INSTALLED_ROOT_UNSAFE",
                "Codex 插件安装目录越出宿主缓存或无法安全解析",
                status_code=409,
                details={"path": str(candidate)},
            ) from exc
        return resolved

    candidates: list[Path] = []
    if version:
        expected = cache / marketplace_name / plugin_name / version
        if expected.is_dir():
            candidates.append(safe_root(expected))
    if cache.is_dir():
        for manifest_path in cache.glob("*/*/*/.codex-plugin/plugin.json"):
            root = safe_root(manifest_path.parent.parent)
            if root not in candidates:
                try:
                    manifest = _load_installed_plugin(root)[1]
                except CodexPluginManifestError:
                    continue
                if manifest.name == plugin_name and (
                    version is None or manifest.version == version or root.name == version
                ):
                    candidates.append(root)
    if len(candidates) != 1:
        raise StudioError(
            "CODEX_PLUGIN_INSTALLED_ROOT_NOT_FOUND",
            "无法唯一定位 Codex App Server 安装的插件目录",
            status_code=409,
            details={
                "marketplaceName": marketplace_name,
                "pluginName": plugin_name,
                "version": version,
                "matches": [str(path) for path in candidates],
            },
        )
    return candidates[0]


__all__ = [
    "CodexPinnedMarketplace",
    "CodexPluginSnapshotStore",
    "CodexWorkspacePluginSnapshot",
    "codex_plugin_ref",
    "component_selector",
    "find_installed_codex_plugin_root",
]
