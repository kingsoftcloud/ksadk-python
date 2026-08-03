"""Deterministic local AgentBundle builder."""

from __future__ import annotations

import hashlib
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ksadk.studio.capabilities import canonical_json, sha256_digest
from ksadk.studio.compiler import AgentCompiler
from ksadk.studio.contracts import (
    AgentDraft,
    BuildRecord,
    BuildStatus,
    BundleManifest,
    FileEntry,
)
from ksadk.studio.repository import BuildRepository
from ksadk.studio.workspace import Workspace

_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


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

    def build(self, draft: AgentDraft) -> BuildRecord:
        compiled = self.compiler.compile(draft)
        short_digest = compiled.resolved.resolved_digest.removeprefix("sha256:")[:20]
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
            self._write_payload(bundle_root, draft, compiled)
            files = self._file_entries(bundle_root)
            manifest = BundleManifest(
                agent_id=draft.metadata.id,
                source_revision=draft.metadata.revision,
                resolved_digest=compiled.resolved.resolved_digest,
                files=files,
            )
            digest_payload = manifest.model_dump(
                by_alias=True,
                exclude={"bundle_digest"},
                exclude_none=True,
                mode="json",
            )
            manifest.bundle_digest = sha256_digest(canonical_json(digest_payload))
            self._write_json(bundle_root / "manifest.json", manifest.model_dump(by_alias=True))
            self._write_checksums(bundle_root)
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
            resolved_digest=compiled.resolved.resolved_digest,
            bundle_digest=manifest.bundle_digest,
            artifact_path=self.workspace.relative(zip_path),
            created_at=now,
            completed_at=now,
        )
        return self.repository.save(record)

    def _write_payload(self, root: Path, draft: AgentDraft, compiled) -> None:
        self._write_json(
            root / "resolved-agent-spec.json",
            compiled.resolved.model_dump(by_alias=True, exclude_none=True, mode="json"),
        )
        self._write_json(root / "agentkit.lock", compiled.dependency_lock)
        instructions = root / "instructions"
        instructions.mkdir()
        (instructions / "system.md").write_text(
            draft.spec.instructions.system.rstrip() + "\n", encoding="utf-8"
        )
        (instructions / "task.md").write_text(
            draft.spec.instructions.task.rstrip() + "\n", encoding="utf-8"
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
        self._write_json(
            root / "provenance.json",
            {
                "format": "agentkit.provenance/v1",
                "agentId": draft.metadata.id,
                "sourceRevision": draft.metadata.revision,
                "sourceDigest": compiled.resolved.source_digest,
                "resolvedDigest": compiled.resolved.resolved_digest,
                "compilerVersion": compiled.resolved.compiler_version,
                "runtimeContract": "agentkit.runtime/v1",
            },
        )

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
