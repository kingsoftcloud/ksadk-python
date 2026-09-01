"""Deterministic local AgentBundle builder."""

from __future__ import annotations

import hashlib
import logging
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ksadk.plugins.bundle_security import assert_bundle_security
from ksadk.plugins.contracts import CompositionProfile, PluginLock, plugin_lock_digest
from ksadk.plugins.resolver import ResolvedComposition
from ksadk.studio.capabilities import canonical_json, compute_bundle_digest, sha256_digest
from ksadk.studio.compatibility_report import (
    build_bundle_compatibility_report,
    compatibility_facts_digest,
)
from ksadk.studio.compiler import AgentCompiler
from ksadk.studio.contracts import (
    AgentDraft,
    BuildRecord,
    BuildStatus,
    BundleManifest,
    FileEntry,
)
from ksadk.studio.hosted_kernel import (
    build_hosted_kernel_requirement,
    hosted_kernel_requirement_digest,
)
from ksadk.studio.repository import BuildRepository
from ksadk.studio.soul import render_soul_markdown
from ksadk.studio.workspace import Workspace

_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

LOGGER = logging.getLogger(__name__)


class AgentBundleBuilder:
    def __init__(
        self,
        workspace: Workspace,
        *,
        compiler: AgentCompiler | None = None,
        repository: BuildRepository | None = None,
    ) -> None:
        self.workspace = workspace
        self.compiler = compiler or AgentCompiler(workspace)
        self.repository = repository or BuildRepository(workspace)

    def build(
        self,
        draft: AgentDraft,
        *,
        composition: ResolvedComposition | None = None,
    ) -> BuildRecord:
        LOGGER.info(
            "bundle build started: agent=%s revision=%s",
            draft.metadata.id,
            draft.metadata.revision,
        )
        compiled = self.compiler.compile(draft)
        # Bundle v2 already carries a deterministic empty lock.  P2-00A now
        # validates that wire shape through PluginLock while deliberately not
        # resolving or loading a PluginHost before P2-02/P2-03.
        parsed_plugin_lock = composition.plugin_lock if composition else PluginLock()
        plugin_lock = parsed_plugin_lock.model_dump(
            by_alias=True,
            exclude_none=True,
            mode="json",
        )
        plugin_lock_digest_value = plugin_lock_digest(parsed_plugin_lock)
        composition_profile = composition.profile if composition else None
        composition_profile_digest_value = composition.profile_digest if composition else None
        runtime_type, source_digest, runtime_lock = self._runtime_snapshot(draft, compiled)
        compatibility_facts_digest_value = compatibility_facts_digest(
            draft=draft,
            composition=composition,
            runtime_lock=runtime_lock,
        )
        resolved_digest_payload = {
            "compatibilityFactsDigest": compatibility_facts_digest_value,
            "definitionDigest": compiled.resolved.resolved_digest,
            "runtime": runtime_lock,
            "sourceDigest": source_digest,
        }
        if composition is not None:
            resolved_digest_payload["compositionProfileDigest"] = composition_profile_digest_value
            resolved_digest_payload["pluginLockDigest"] = plugin_lock_digest_value
        resolved_digest = sha256_digest(canonical_json(resolved_digest_payload))
        short_digest = resolved_digest.removeprefix("sha256:")[:20]
        build_id = f"build_{short_digest}"
        final_dir = self.workspace.resolve(Path("dist") / draft.metadata.id / build_id)
        zip_path = final_dir / "agent-bundle.zip"
        if zip_path.is_file():
            return self.repository.get(build_id)

        staging = self.workspace.resolve(
            Path(".agentkit/builds") / f".{build_id}.{uuid4().hex}.tmp"
        )
        bundle_root = staging / "agent-bundle"
        bundle_root.mkdir(parents=True, exist_ok=False)
        try:
            self._copy_runtime_source(bundle_root, draft)
            self._write_runtime_launch_config(bundle_root, draft)
            launch_config = bundle_root / "runtime" / "agentengine.yaml"
            hosted_kernel_requirement = build_hosted_kernel_requirement(
                runtime_type=runtime_type,
                entry_point=runtime_lock.get("entryPoint"),
                agent_variable=runtime_lock.get("agentVariable"),
                launch_config=launch_config.read_bytes() if launch_config.is_file() else None,
            )
            hosted_kernel_requirement_digest_value = hosted_kernel_requirement_digest(
                hosted_kernel_requirement
            )
            self._write_payload(
                bundle_root,
                draft,
                compiled,
                runtime_lock=runtime_lock,
                resolved_digest=resolved_digest,
                plugin_lock=plugin_lock,
                composition_profile=composition_profile,
                composition_profile_digest_value=composition_profile_digest_value,
                hosted_kernel_requirement=hosted_kernel_requirement,
                hosted_kernel_requirement_digest_value=hosted_kernel_requirement_digest_value,
                compatibility_facts_digest_value=compatibility_facts_digest_value,
            )
            self._write_json(
                bundle_root / "compatibility-report.json",
                build_bundle_compatibility_report(
                    draft=draft,
                    composition=composition,
                    runtime_lock=runtime_lock,
                    resolved_digest=resolved_digest,
                    facts_digest=compatibility_facts_digest_value,
                    plugin_lock_digest=plugin_lock_digest_value,
                    composition_profile_digest=composition_profile_digest_value,
                ),
            )
            self._write_json(
                bundle_root / "hosted-kernel-requirements.json",
                hosted_kernel_requirement,
            )
            # Secrets are references at every declarative boundary.  Scan the
            # final materialized Bundle before its file manifest/digest are
            # sealed, so an accidental literal cannot become a deployable ZIP.
            assert_bundle_security(bundle_root)
            # The manifest is a complete content declaration. Write this
            # auxiliary checksum file first, then include it in the manifest
            # entries; otherwise a Server-side full-membership check correctly
            # rejects the archive as self-inconsistent.
            self._write_checksums(bundle_root)
            files = self._file_entries(bundle_root)
            manifest = BundleManifest(
                bundle_format="agentkit.bundle/v2",
                agent_id=draft.metadata.id,
                source_revision=draft.metadata.revision,
                resolved_digest=resolved_digest,
                runtime_type=runtime_type,
                source_digest=source_digest,
                plugin_lock_digest=plugin_lock_digest_value,
                composition_mode="composed" if composition is not None else "legacy",
                composition_profile_digest=composition_profile_digest_value,
                hosted_kernel_requirement_digest=hosted_kernel_requirement_digest_value,
                files=files,
            )
            manifest.bundle_digest = compute_bundle_digest(manifest)
            # Keep the on-disk manifest on the same ``exclude_none`` wire
            # projection used by ``compute_bundle_digest``.  New v2 archives
            # always state whether they select legacy or composed execution;
            # only historical archives may omit that discriminator.
            self._write_json(
                bundle_root / "manifest.json",
                manifest.model_dump(by_alias=True, exclude_none=True),
            )
            archive = staging / "agent-bundle.zip"
            self._write_zip(bundle_root, archive)
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            final_dir.mkdir(parents=True, exist_ok=False)
            shutil.move(str(bundle_root), str(final_dir / "agent-bundle"))
            shutil.move(str(archive), str(zip_path))
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        now = datetime.now(timezone.utc)
        record = BuildRecord(
            id=build_id,
            agent_id=draft.metadata.id,
            source_revision=draft.metadata.revision,
            status=BuildStatus.SUCCEEDED,
            resolved_digest=resolved_digest,
            runtime_type=runtime_type,
            source_digest=source_digest,
            runtime_lock=runtime_lock,
            bundle_digest=manifest.bundle_digest,
            artifact_path=self.workspace.relative(zip_path),
            created_at=now,
            completed_at=now,
        )
        saved = self.repository.save(record)
        LOGGER.info(
            "bundle build finished: agent=%s build=%s artifact=%s",
            draft.metadata.id,
            saved.id,
            saved.artifact_path,
        )
        return saved

    def _write_payload(
        self,
        root: Path,
        draft: AgentDraft,
        compiled,
        *,
        runtime_lock: dict,
        resolved_digest: str,
        plugin_lock: dict,
        composition_profile: CompositionProfile | None,
        composition_profile_digest_value: str | None,
        hosted_kernel_requirement: dict,
        hosted_kernel_requirement_digest_value: str,
        compatibility_facts_digest_value: str,
    ) -> None:
        definition_digest = compiled.resolved.resolved_digest
        resolved_payload = compiled.resolved.model_dump(
            by_alias=True,
            exclude_none=True,
            mode="json",
        )
        resolved_payload["resolvedDigest"] = resolved_digest
        dependency_lock = dict(compiled.dependency_lock)
        dependency_lock["definitionDigest"] = definition_digest
        dependency_lock["resolvedDigest"] = resolved_digest
        self._write_json(
            root / "resolved-agent-spec.json",
            resolved_payload,
        )
        self._write_json(root / "agentkit.lock", dependency_lock)
        self._write_json(root / "runtime-lock.json", runtime_lock)
        self._write_json(root / "plugin-lock.json", plugin_lock)
        if composition_profile is not None:
            # A resolved profile is an immutable composition input, not a
            # second editable Agent spec.  It is only written once its
            # deterministic lock has already been resolved by PluginRegistry.
            self._write_json(
                root / "composition-profile.json",
                composition_profile.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
            )
        instructions = root / "instructions"
        instructions.mkdir()
        (instructions / "system.md").write_text(
            compiled.resolved.instructions.system.rstrip() + "\n", encoding="utf-8"
        )
        (instructions / "task.md").write_text(
            compiled.resolved.instructions.task.rstrip() + "\n", encoding="utf-8"
        )
        if compiled.resolved.soul is not None:
            (instructions / "soul.md").write_text(
                render_soul_markdown(compiled.resolved.soul), encoding="utf-8"
            )
        for skill in compiled.resolved.capabilities.skills:
            source = self.workspace.resolve(
                Path("capabilities/skills") / skill["name"],
                must_exist=True,
            )
            target = root / skill["bundlePath"]
            shutil.copytree(
                source,
                target,
                symlinks=False,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
            )
        components = []
        for kind, values in (
            ("skill", compiled.resolved.capabilities.skills),
            ("mcp", compiled.resolved.capabilities.mcp_servers),
        ):
            for value in values:
                components.append(
                    {
                        "type": kind,
                        "name": value["name"],
                        "version": value["version"],
                        "digest": value["digest"],
                    }
                )
        for tool in compiled.resolved.capabilities.tools:
            components.append(
                {
                    "type": "tool",
                    "name": tool.name,
                    "version": tool.version,
                    "digest": tool.digest,
                }
            )
        components.sort(key=lambda item: (item["type"], item["name"], item["version"]))
        self._write_json(
            root / "sbom.spdx.json",
            {
                "spdxVersion": "SPDX-2.3",
                "name": f"{draft.metadata.id}-agent-bundle",
                "components": components,
            },
        )
        provenance = {
            "format": "agentkit.provenance/v1",
            "agentId": draft.metadata.id,
            "sourceRevision": draft.metadata.revision,
            "sourceDigest": compiled.resolved.source_digest,
            "definitionDigest": definition_digest,
            "resolvedDigest": resolved_digest,
            "compilerVersion": compiled.resolved.compiler_version,
            "runtimeContract": "agentkit.runtime/v1",
            "compatibility": {
                "reportPath": "compatibility-report.json",
                "factsDigest": compatibility_facts_digest_value,
            },
            "hostedKernel": {
                "requirementsPath": "hosted-kernel-requirements.json",
                "requirementDigest": hosted_kernel_requirement_digest_value,
                "contractSet": hosted_kernel_requirement["kernelContract"]["set"],
                "contractDigest": hosted_kernel_requirement["kernelContract"]["digest"],
            },
        }
        if composition_profile is not None:
            provenance["composition"] = {
                "profilePath": "composition-profile.json",
                "profileDigest": composition_profile_digest_value,
                "pluginLockDigest": sha256_digest(canonical_json(plugin_lock)),
            }
        self._write_json(root / "provenance.json", provenance)

    def _runtime_snapshot(self, draft: AgentDraft, compiled) -> tuple[str, str, dict]:
        runtime = draft.spec.runtime
        runtime_type = runtime.type if runtime is not None else ""
        source_digest = ""
        source_files: list[dict[str, object]] = []
        if runtime is not None and runtime.project_path:
            source_root = self.workspace.resolve(runtime.project_path, must_exist=True)
            for path in self._source_files(source_root):
                content = path.read_bytes()
                relative = path.relative_to(source_root).as_posix()
                digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
                source_files.append({"path": relative, "sha256": digest, "size": len(content)})
            source_digest = sha256_digest(canonical_json(source_files))
        bound_models = [
            item.model for item in self.compiler.catalog.resolve_models(draft.spec.bindings)
        ]
        if compiled.resolved.model.model not in bound_models:
            bound_models.insert(0, compiled.resolved.model.model)
        lock = {
            "type": runtime_type,
            "projectPath": runtime.project_path if runtime is not None else None,
            "entryPoint": runtime.entry_point if runtime is not None else None,
            "agentVariable": runtime.agent_variable if runtime is not None else None,
            "version": runtime.version if runtime is not None else None,
            "detection": runtime.detection if runtime is not None else None,
            "sourceDigest": source_digest,
            "definitionDigest": compiled.resolved.resolved_digest,
            "model": compiled.resolved.model.model,
            "models": list(dict.fromkeys(bound_models)),
        }
        return (
            runtime_type,
            source_digest,
            {key: value for key, value in lock.items() if value is not None},
        )

    def _copy_runtime_source(self, bundle_root: Path, draft: AgentDraft) -> None:
        runtime = draft.spec.runtime
        if runtime is None or not runtime.project_path:
            return
        source_root = self.workspace.resolve(runtime.project_path, must_exist=True)
        target_root = bundle_root / "runtime"
        for source in self._source_files(source_root):
            relative = source.relative_to(source_root)
            target = target_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())

    def _write_runtime_launch_config(self, bundle_root: Path, draft: AgentDraft) -> None:
        """Make the copied runtime source directly launchable by the profile image.

        Production Code deployments execute the runtime directory through the
        KsADK web command. The source snapshot therefore needs an explicit,
        immutable framework declaration rather than relying on heuristics or a
        user-supplied YAML that could disagree with the admitted runtime lock.
        """

        runtime = draft.spec.runtime
        if runtime is None or not runtime.project_path:
            return
        self._write_json(
            bundle_root / "runtime" / "agentengine.yaml",
            {
                "name": draft.metadata.id,
                "framework": runtime.type,
                "entry_point": runtime.entry_point or "agent.py",
                "agent_variable": runtime.agent_variable or "root_agent",
                "package": ".",
            },
        )

    @staticmethod
    def _source_files(root: Path) -> list[Path]:
        files: list[Path] = []
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_symlink():
                raise ValueError(f"Runtime source cannot contain symlinks: {path}")
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if any(part in {"__pycache__", ".git", ".venv"} for part in relative.parts):
                continue
            if path.suffix in {".pyc", ".pyo"}:
                continue
            files.append(path)
        return files

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        path.write_bytes(canonical_json(payload) + b"\n")

    @staticmethod
    def _file_entries(root: Path) -> list[FileEntry]:
        entries = []
        for path in sorted(
            (item for item in root.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(root).as_posix(),
        ):
            content = path.read_bytes()
            entries.append(
                FileEntry(
                    path=path.relative_to(root).as_posix(),
                    sha256=f"sha256:{hashlib.sha256(content).hexdigest()}",
                    size=len(content),
                )
            )
        return entries

    @staticmethod
    def _write_checksums(root: Path) -> None:
        lines = []
        for path in sorted(
            (item for item in root.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(root).as_posix(),
        ):
            relative = path.relative_to(root).as_posix()
            lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}")
        (root / "checksums.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _write_zip(root: Path, target: Path) -> None:
        with zipfile.ZipFile(
            target,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for path in sorted(
                (item for item in root.rglob("*") if item.is_file()),
                key=lambda item: item.relative_to(root).as_posix(),
            ):
                relative = path.relative_to(root).as_posix()
                info = zipfile.ZipInfo(relative, _ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes(), compresslevel=9)
