"""File-backed repositories for Agent drafts and immutable build records."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from ksadk.studio.contracts import (
    AgentAppearance,
    AgentDraft,
    AgentMetadata,
    AgentSpec,
    BuildRecord,
    Instructions,
)
from ksadk.studio.errors import StudioError, not_found
from ksadk.studio.workspace import Workspace


class AgentDraftRepository:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def _agent_dir(self, agent_id: str) -> Path:
        return self.workspace.resolve(Path("agents") / agent_id)

    def _agent_file(self, agent_id: str) -> Path:
        return self._agent_dir(agent_id) / "agent.yaml"

    def create(
        self,
        *,
        agent_id: str,
        name: str,
        description: str = "",
        template: str = "blank",
        spec: AgentSpec | None = None,
        labels: dict[str, str] | None = None,
    ) -> AgentDraft:
        if template not in {"blank", "research"}:
            raise StudioError(
                "AGENT_TEMPLATE_UNSUPPORTED",
                "不支持的 Agent 模板",
                status_code=422,
                field="template",
                details={"template": template},
            )
        draft = AgentDraft(
            metadata=AgentMetadata(
                id=agent_id,
                name=name,
                labels={
                    "agentkit.ksyun.com/template": template,
                    **dict(labels or {}),
                },
            ),
            spec=spec
            or AgentSpec(
                description=description,
                instructions=Instructions(
                    system="你是一个可靠的企业智能助手。",
                    task="",
                ),
            ),
        )
        path = self._agent_file(agent_id)
        if path.exists():
            raise StudioError(
                "AGENT_ALREADY_EXISTS",
                "Agent ID 已存在",
                status_code=409,
                details={"id": agent_id},
            )
        path.parent.mkdir(parents=True, exist_ok=False)
        (path.parent / "instructions").mkdir()
        (path.parent / "evaluations").mkdir()
        self._write(path, draft)
        return draft

    def list(self, *, query: str = "", limit: int = 50) -> list[AgentDraft]:
        normalized = query.strip().lower()
        results: list[AgentDraft] = []
        agents_dir = self.workspace.resolve("agents")
        for path in sorted(agents_dir.glob("*/agent.yaml")):
            try:
                draft = self._read(path)
            except StudioError:
                continue
            if (
                normalized
                and normalized not in draft.metadata.id.lower()
                and normalized not in (draft.metadata.name.lower())
            ):
                continue
            results.append(draft)
            if len(results) >= limit:
                break
        return results

    def get(self, agent_id: str) -> AgentDraft:
        path = self._agent_file(agent_id)
        if not path.is_file():
            raise not_found("agent", agent_id)
        return self._read(path)

    def update(self, agent_id: str, spec: AgentSpec, *, expected_revision: int) -> AgentDraft:
        current = self.get(agent_id)
        if current.metadata.revision != expected_revision:
            raise StudioError(
                "AGENT_REVISION_CONFLICT",
                "Agent 已被其他操作更新",
                status_code=409,
                field="metadata.revision",
                details={
                    "expected": expected_revision,
                    "actual": current.metadata.revision,
                },
            )
        updated = cast(AgentDraft, current.model_copy(deep=True))
        updated.metadata.revision += 1
        updated.spec = spec
        self._write(self._agent_file(agent_id), updated)
        return updated

    def update_appearance(
        self,
        agent_id: str,
        appearance: AgentAppearance,
        *,
        expected_revision: int,
    ) -> AgentDraft:
        current = self.get(agent_id)
        if current.metadata.revision != expected_revision:
            raise StudioError(
                "AGENT_REVISION_CONFLICT",
                "Agent 已被其他操作更新",
                status_code=409,
                field="metadata.revision",
                details={"expected": expected_revision, "actual": current.metadata.revision},
            )
        updated = cast(AgentDraft, current.model_copy(deep=True))
        updated.metadata.revision += 1
        updated.metadata.appearance = appearance
        self._write(self._agent_file(agent_id), updated)
        return updated

    def replace(self, draft: AgentDraft) -> AgentDraft:
        """Persist metadata-only creation state without creating a Revision."""

        current = self.get(draft.metadata.id)
        if draft.metadata.revision != current.metadata.revision:
            raise StudioError(
                "AGENT_REVISION_CONFLICT",
                "Agent metadata replacement revision mismatch",
                status_code=409,
            )
        self._write(self._agent_file(draft.metadata.id), draft)
        return draft

    def delete(
        self,
        agent_id: str,
        *,
        purge: bool = False,
        trash_directory: Path | None = None,
    ) -> None:
        agent_dir = self._agent_dir(agent_id)
        if not agent_dir.is_dir():
            raise not_found("agent", agent_id)
        if purge:
            shutil.rmtree(agent_dir)
            return
        if trash_directory is None:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            trash = self.workspace.resolve(Path(".agentkit/trash") / f"{agent_id}-{timestamp}")
        else:
            trash = self.workspace.resolve(trash_directory / "source" / "agents" / agent_id)
        trash.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(agent_dir), str(trash))

    def _read(self, path: Path) -> AgentDraft:
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
            return cast(AgentDraft, AgentDraft.model_validate(payload))
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise StudioError(
                "AGENT_SCHEMA_INVALID",
                "Agent YAML 无法解析",
                status_code=422,
                details={"path": self.workspace.relative(path), "reason": str(exc)},
            ) from exc

    def _write(self, path: Path, draft: AgentDraft) -> None:
        self.workspace.atomic_write_yaml(
            path,
            draft.model_dump(by_alias=True, exclude_none=True, mode="json"),
        )


class BuildRepository:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def _path(self, build_id: str) -> Path:
        return self.workspace.resolve(Path(".agentkit/builds") / f"{build_id}.json")

    def save(self, record: BuildRecord) -> BuildRecord:
        payload = record.model_dump(by_alias=True, exclude_none=True, mode="json")
        self.workspace.atomic_write_text(
            self._path(record.id),
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        return record

    def get(self, build_id: str) -> BuildRecord:
        path = self._path(build_id)
        if not path.is_file():
            raise not_found("build", build_id)
        try:
            return cast(
                BuildRecord,
                BuildRecord.model_validate_json(path.read_text(encoding="utf-8")),
            )
        except (OSError, ValidationError) as exc:
            raise StudioError(
                "BUILD_RECORD_INVALID",
                "Build 记录损坏",
                status_code=500,
                details={"id": build_id},
            ) from exc

    def list_for_agent(self, agent_id: str) -> list[BuildRecord]:
        records: list[BuildRecord] = []
        directory = self.workspace.resolve(".agentkit/builds")
        for path in sorted(directory.glob("build_*.json"), reverse=True):
            try:
                record = BuildRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValidationError):
                continue
            if record.agent_id == agent_id:
                records.append(record)
        return records

    def delete_for_agent(
        self,
        agent_id: str,
        *,
        purge: bool,
        trash_directory: Path | None = None,
    ) -> int:
        records = self.list_for_agent(agent_id)
        artifact_directories: set[Path] = set()
        for record in records:
            if record.artifact_path:
                archive = self.workspace.resolve(record.artifact_path)
                artifact_directories.add(archive.parent)
        for directory in artifact_directories:
            if not directory.is_dir():
                continue
            if purge:
                shutil.rmtree(directory)
            else:
                if trash_directory is None:
                    raise ValueError("recoverable deletion requires a trash directory")
                destination = self.workspace.resolve(trash_directory / "artifacts" / directory.name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(directory), str(destination))
        for record in records:
            path = self._path(record.id)
            if not path.is_file():
                continue
            if purge:
                path.unlink()
            else:
                if trash_directory is None:
                    raise ValueError("recoverable deletion requires a trash directory")
                destination = self.workspace.resolve(trash_directory / "builds" / path.name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(destination))
        return len(records)


def load_yaml_file(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise StudioError(
            "WORKSPACE_INVALID",
            "YAML 文件无法解析",
            status_code=422,
            details={"path": str(path), "reason": str(exc)},
        ) from exc
    if not isinstance(payload, dict):
        raise StudioError(
            "WORKSPACE_INVALID",
            "YAML 根节点必须是对象",
            status_code=422,
            details={"path": str(path)},
        )
    return cast(dict[str, Any], payload)
