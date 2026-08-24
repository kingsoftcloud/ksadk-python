"""Local, atomic bindings between workspace EvalSets and cloud Dataset versions."""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from pydantic import Field, field_validator

from .contracts import EvaluationModel


class CloudBindingError(RuntimeError):
    """Raised when a cloud binding cannot be read or safely persisted."""


class CloudBinding(EvaluationModel):
    """Non-sensitive reference to one immutable cloud Dataset version."""

    schema_version: str = "ksadk.eval.cloud/v1"
    evalset_path: str = Field(min_length=1)
    content_digest: str = Field(min_length=64, max_length=64)
    provider: str = Field(min_length=1)
    project_id: str | None = None
    dataset_id: str = Field(min_length=1)
    dataset_version: int = Field(ge=1)
    schema_hash: str = Field(min_length=64, max_length=64)

    @field_validator("evalset_path")
    @classmethod
    def validate_evalset_path(cls, value: str) -> str:
        return _workspace_relative_path(value)


class CloudBindingStore:
    """Persist bindings under the workspace without accepting arbitrary output paths."""

    def __init__(self, workspace_root: str | Path):
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.root = self.workspace_root / ".agentkit" / "evaluation-bindings"

    def binding_path(self, evalset_path: str) -> Path:
        normalized = _workspace_relative_path(evalset_path)
        file_name = hashlib.sha256(normalized.encode("utf-8")).hexdigest() + ".yaml"
        return self.root / file_name

    def write(self, binding: CloudBinding) -> Path:
        path = self.binding_path(binding.evalset_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        payload = yaml.safe_dump(
            binding.model_dump(mode="json", by_alias=True, exclude_none=True),
            allow_unicode=True,
            sort_keys=True,
        )
        try:
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise CloudBindingError("云端评测集绑定写入失败") from exc
        return path

    def read(self, evalset_path: str) -> CloudBinding | None:
        path = self.binding_path(evalset_path)
        if not path.is_file():
            return None
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            return CloudBinding.model_validate(loaded)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise CloudBindingError("云端评测集绑定损坏或不可读") from exc


def _workspace_relative_path(value: str) -> str:
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise CloudBindingError("EvalSet 路径必须位于工作区内")
    return candidate.as_posix()
