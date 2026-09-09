"""Offline, Build-bound Skill directory and text resources for plugin consumers."""

from __future__ import annotations

import os
import stat
from dataclasses import replace
from pathlib import Path, PurePosixPath

from pydantic import Field, field_validator

from ksadk.plugins.contracts import PluginContractModel
from ksadk.resource_runtime.build_artifacts import ResourceBuildReference, restore_resource_build
from ksadk.skills.loader import load_local_skill
from ksadk.skills.package_store import SkillPackageError
from ksadk.skills.runtime.base import SkillRuntimeBackend, SkillRuntimeResult
from ksadk.skills.runtime.registry import match_skill_refs


class SkillSelector(PluginContractModel):
    skill_id: str = Field(strict=True, min_length=1, max_length=256)


class SkillSearchQuery(PluginContractModel):
    query: str = Field(strict=True, min_length=1, max_length=16000)
    max_results: int = Field(default=10, strict=True, ge=1, le=32)


class SkillExecutionQuery(PluginContractModel):
    workflow_prompt: str = Field(strict=True, min_length=1, max_length=16000)
    skill_ids: tuple[str, ...] = Field(min_length=1, max_length=32)

    @field_validator("skill_ids")
    @classmethod
    def unique_ids(cls, values):
        if len(set(values)) != len(values) or any(not item or len(item) > 256 for item in values):
            raise ValueError("Execution requires unique Skill IDs")
        return values


class SkillResourceQuery(SkillSelector):
    path: str = Field(strict=True, min_length=1, max_length=1024)
    offset: int = Field(default=0, strict=True, ge=0, le=1024 * 1024)
    max_chars: int = Field(default=16000, strict=True, ge=1, le=64000)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or "\\" in value
            or "\x00" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("Skill resource must use a canonical relative path")
        return value


class PinnedSkillService:
    """The host supplies an admitted Build and binding; no remote catalog fallback.

    Every operation revalidates saved Build bytes and the extraction cache. This
    protects reproducibility, not hostile same-user processes during a read.
    Paths returned in tool results are package-relative, never host locations.
    """

    def __init__(
        self,
        directory: Path,
        reference: ResourceBuildReference,
        *,
        binding_id: str,
        cache_directory: Path,
    ):
        self.directory = directory
        self.reference = ResourceBuildReference.model_validate(reference.model_dump())
        self.binding_id = binding_id
        self.cache_directory = cache_directory
        self._packages()

    def _restore(self):
        manifest, packages = restore_resource_build(
            self.directory,
            self.reference,
            cache_directory=self.cache_directory,
        )
        binding = manifest.snapshot.binding(self.binding_id)
        config = binding.config
        if config.binding.resource.kind != "skill-space" or config.selection_mode == "discovery":
            raise SkillPackageError("Pinned Skill service requires a pinned Skill binding")
        return (
            config,
            {package.ref.skill_id: package for package in packages.get(self.binding_id, [])},
            binding.skill_execution,
        )

    def _packages(self):
        return self._restore()[1]

    def execute(
        self,
        arguments: dict,
        *,
        backend: SkillRuntimeBackend,
        operation_id: str,
        timeout: int,
    ) -> SkillRuntimeResult:
        """Host-only post-approval adapter to an explicitly admitted Runtime.

        The host selects the backend and owns artifact publication. The internal
        result may contain host paths; it must not be sent directly as tool text.
        This adapter grants no approval and does not select a backend from env.
        """
        query = SkillExecutionQuery.model_validate(arguments)
        if not query.workflow_prompt.strip() or type(timeout) is not int or not 1 <= timeout <= 900:
            raise ValueError("Invalid Skill execution budget or prompt")
        if len(operation_id) != 64 or any(char not in "0123456789abcdef" for char in operation_id):
            raise ValueError("Execution requires a durable operation ID")
        config, packages, target = self._restore()
        if config.execution_mode != "isolated":
            raise ValueError("SKILL_EXECUTION_MODE_UNSUPPORTED")
        if any(skill_id not in packages for skill_id in query.skill_ids):
            raise ValueError("SKILL_NOT_BOUND")
        selected = [packages[skill_id] for skill_id in query.skill_ids]
        return backend.run_workflow(
            query.workflow_prompt,
            skill_space_ids=[],
            session_id=operation_id,
            skill_names=[package.ref.name for package in selected],
            pinned_packages=selected,
            timeout=min(timeout, target.timeout) if target is not None else timeout,
        )

    def list_skills(self, arguments: dict) -> dict:
        if arguments:
            raise ValueError("Pinned directory accepts no resource or space parameters")
        packages = self._packages()
        return {
            "status": "ok",
            "items": [
                {
                    "skillId": package.ref.skill_id,
                    "versionId": package.ref.version_id,
                    "name": package.ref.name,
                    "contentHash": package.ref.content_hash.render(),
                }
                for package in sorted(packages.values(), key=lambda item: item.ref.skill_id)
            ],
            "truncated": False,
        }

    def read_resource(self, arguments: dict) -> dict:
        query = SkillResourceQuery.model_validate(arguments)
        package = self._packages().get(query.skill_id)
        if package is None:
            raise ValueError("SKILL_NOT_BOUND")
        path = package.root_dir
        for part in PurePosixPath(query.path).parts:
            path = path / part
            if path.is_symlink():
                raise SkillPackageError("Skill resource cannot be a symbolic link")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                raise SkillPackageError("Skill text resource exceeds limit or is not a file")
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise SkillPackageError("Skill text resource exceeds limit")
        content = raw.decode("utf-8")
        end = min(len(content), query.offset + query.max_chars)
        truncated = end < len(content)
        return {
            "status": "ok",
            "skillId": query.skill_id,
            "versionId": package.ref.version_id,
            "contentHash": package.ref.content_hash.render(),
            "path": query.path,
            "content": content[query.offset : end],
            "truncated": truncated,
            "nextOffset": end if truncated else None,
        }

    def search_skills(self, arguments: dict) -> dict:
        query = SkillSearchQuery.model_validate(arguments)
        if not query.query.strip():
            raise ValueError("Skill query cannot be blank")
        refs = []
        for package in self._packages().values():
            if (package.root_dir / "SKILL.md").stat().st_size > 1024 * 1024:
                raise SkillPackageError("Skill document exceeds text limit")
            document = load_local_skill(package.root_dir)
            refs.append(replace(package.ref, description=document.description[:8192]))
        matches = match_skill_refs(refs, query.query)
        return {
            "status": "ok",
            "truncated": len(matches) > query.max_results,
            "items": [
                {
                    "skillId": match.skill.skill_id,
                    "name": match.skill.name,
                    "versionId": match.skill.version_id,
                    "contentHash": match.skill.content_hash.render(),
                    "description": match.skill.description,
                    "score": match.score,
                    "reason": match.reason,
                }
                for match in matches[: query.max_results]
            ],
        }

    def load_skill(self, arguments: dict) -> dict:
        selector = SkillSelector.model_validate(arguments)
        return self.read_resource({"skillId": selector.skill_id, "path": "SKILL.md"})
