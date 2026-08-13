"""Codex Studio 的单一 ``agentengine.yaml`` 合同与文件仓储。"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ksadk.builders.managed_runtime_builder import serialize_managed_runtime_manifest
from ksadk.studio.errors import StudioError, not_found
from ksadk.studio.workspace import Workspace


class CodexRuntimeRef(BaseModel):
    """平台托管的 Codex runtime 锁定信息。"""

    model_config = ConfigDict(extra="forbid")

    name: Literal["codex"] = "codex"
    version: str = Field(min_length=1, max_length=64)


class CodexAgentManifest(BaseModel):
    """本地演示允许进入 ``agentengine.yaml`` 的完整字段集合。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,62}$")
    version: str = Field(min_length=1, max_length=64)
    framework: Literal["codex"] = "codex"
    artifact_type: Literal["ManagedRuntime"] = "ManagedRuntime"
    runtime: CodexRuntimeRef
    model: str = Field(min_length=1, max_length=256)
    models: list[str] | None = None
    prompt: str = Field(min_length=1, max_length=32768)
    skills: list[str] | None = None
    mcp_servers: list[dict[str, Any]] | None = None
    sandbox: str | None = None
    approval_mode: str | None = None

    @model_validator(mode="after")
    def validate_models(self) -> "CodexAgentManifest":
        if self.models is None:
            return self
        normalized: list[str] = []
        for value in self.models:
            model = str(value).strip()
            if not model or len(model) > 256:
                raise ValueError("models 中的模型名称长度必须为 1..256")
            if model in normalized:
                raise ValueError("models 不能包含重复模型")
            normalized.append(model)
        if not normalized:
            raise ValueError("models 至少包含一个模型")
        if self.model not in normalized:
            raise ValueError("默认模型 model 必须包含在 models 中")
        self.models = normalized
        if self.skills is not None:
            seen: set[str] = set()
            deduped: list[str] = []
            for sid in self.skills:
                sid = str(sid).strip()
                if not sid:
                    raise ValueError("skills 不能包含空值")
                if sid in seen:
                    raise ValueError("skills 不能包含重复资源")
                seen.add(sid)
                deduped.append(sid)
            self.skills = deduped
        if self.mcp_servers is not None:
            mcp_seen: set[str] = set()
            mcp_deduped: list[dict[str, Any]] = []
            for server in self.mcp_servers:
                if not isinstance(server, dict):
                    raise ValueError("mcp_servers 必须是对象列表")
                name = str(server.get("name") or "").strip()
                url = str(server.get("url") or "").strip()
                if not name:
                    raise ValueError("mcp_servers 每项必须有 name")
                if not url:
                    raise ValueError("mcp_servers 每项必须有 url")
                if name in mcp_seen:
                    raise ValueError("mcp_servers 不能包含重复 name")
                mcp_seen.add(name)
                mcp_deduped.append(server)
            self.mcp_servers = mcp_deduped
        return self

    @property
    def allowed_models(self) -> tuple[str, ...]:
        return tuple(self.models or [self.model])


def normalized_manifest_bytes(manifest: CodexAgentManifest) -> bytes:
    """生成构建、SHA 和磁盘写入共同使用的规范化 YAML。"""

    payload = manifest.model_dump(mode="python", exclude_none=True)
    return serialize_managed_runtime_manifest(payload)


@dataclass(frozen=True)
class CodexManifestSnapshot:
    manifest: CodexAgentManifest
    manifest_sha256: str
    source_path: Path
    source_bytes: bytes


_AGENT_NAME = re.compile(r"^[a-z][a-z0-9-]{2,62}$")


class CodexManifestRepository:
    """每个本地 Agent 一份 YAML，首个 Agent 兼容工作区根路径。"""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.path = workspace.resolve("agentengine.yaml")
        self.agents_path = workspace.resolve("agents")

    def exists(self, agent_id: str | None = None) -> bool:
        if agent_id is None:
            return self.path.is_file()
        try:
            self.load(agent_id)
        except StudioError as exc:
            if exc.status_code == 404:
                return False
            raise
        return True

    def load(self, agent_id: str | None = None) -> CodexManifestSnapshot:
        if agent_id is None:
            return self._load_path(self.path)
        self._validate_agent_id(agent_id)
        if self.path.is_file():
            root = self._load_path(self.path)
            if root.manifest.name == agent_id:
                return root
        path = self._agent_path(agent_id)
        snapshot = self._load_path(path, agent_id=agent_id)
        if snapshot.manifest.name != agent_id:
            raise StudioError(
                "CODEX_MANIFEST_ID_MISMATCH",
                "Agent 目录与 agentengine.yaml 中的 name 不一致",
                status_code=422,
                details={"agentId": agent_id, "manifestName": snapshot.manifest.name},
            )
        return snapshot

    def list(self) -> list[CodexManifestSnapshot]:
        snapshots: list[CodexManifestSnapshot] = []
        seen: set[str] = set()
        if self.path.is_file():
            root = self._load_path(self.path)
            snapshots.append(root)
            seen.add(root.manifest.name)
        for path in sorted(self.agents_path.glob("*/agentengine.yaml")):
            try:
                snapshot = self._load_path(path)
            except StudioError:
                continue
            if snapshot.manifest.name in seen:
                continue
            snapshots.append(snapshot)
            seen.add(snapshot.manifest.name)
        return snapshots

    def _load_path(
        self,
        path: Path,
        *,
        agent_id: str | None = None,
    ) -> CodexManifestSnapshot:
        if not path.is_file():
            if agent_id is not None:
                raise not_found("agent", agent_id)
            raise StudioError(
                "CODEX_MANIFEST_NOT_FOUND",
                "当前工作区还没有 agentengine.yaml",
                status_code=404,
            )
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
            manifest = cast(
                CodexAgentManifest,
                CodexAgentManifest.model_validate(payload),
            )
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise StudioError(
                "CODEX_MANIFEST_INVALID",
                "agentengine.yaml 不符合 Codex ManagedRuntime 合同",
                status_code=422,
                details={"reason": str(exc)},
            ) from exc
        source = normalized_manifest_bytes(manifest)
        return CodexManifestSnapshot(
            manifest=manifest,
            manifest_sha256=hashlib.sha256(source).hexdigest(),
            source_path=path,
            source_bytes=source,
        )

    def save(self, manifest: CodexAgentManifest) -> CodexManifestSnapshot:
        source = normalized_manifest_bytes(manifest)
        path = self._save_path(manifest.name)
        self.workspace.atomic_write_text(path, source.decode("utf-8"))
        return CodexManifestSnapshot(
            manifest=manifest,
            manifest_sha256=hashlib.sha256(source).hexdigest(),
            source_path=path,
            source_bytes=source,
        )

    def delete(
        self,
        agent_id: str,
        *,
        purge: bool,
        trash_directory: Path | None = None,
    ) -> None:
        """Remove one Agent source without ever deleting the workspace root."""

        snapshot = self.load(agent_id)
        source = snapshot.source_path
        if source == self.path:
            if purge:
                source.unlink()
                return
            if trash_directory is None:
                raise ValueError("recoverable deletion requires a trash directory")
            destination = self.workspace.resolve(trash_directory / "source/agentengine.yaml")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            return

        agent_directory = source.parent
        if purge:
            shutil.rmtree(agent_directory)
            return
        if trash_directory is None:
            raise ValueError("recoverable deletion requires a trash directory")
        destination = self.workspace.resolve(
            trash_directory / "source/agents" / agent_id
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(agent_directory), str(destination))

    def _save_path(self, agent_id: str) -> Path:
        self._validate_agent_id(agent_id)
        if not self.path.is_file():
            return self.path
        if self._load_path(self.path).manifest.name == agent_id:
            return self.path
        return self._agent_path(agent_id)

    def _agent_path(self, agent_id: str) -> Path:
        self._validate_agent_id(agent_id)
        return self.workspace.resolve(Path("agents") / agent_id / "agentengine.yaml")

    @staticmethod
    def _validate_agent_id(agent_id: str) -> None:
        if not _AGENT_NAME.fullmatch(agent_id):
            raise not_found("agent", agent_id)


__all__ = [
    "CodexAgentManifest",
    "CodexManifestRepository",
    "CodexManifestSnapshot",
    "CodexRuntimeRef",
    "normalized_manifest_bytes",
]
