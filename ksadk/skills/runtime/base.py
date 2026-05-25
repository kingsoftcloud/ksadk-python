from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence


class SkillRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class SandboxInputFile:
    source: Path
    target_path: str


@dataclass(frozen=True)
class SkillRuntimeResult:
    runtime_id: str = ""
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    timed_out: bool = False
    error_type: str | None = None
    error_message: str | None = None
    output_files: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.error_type and not self.timed_out

    def to_dict(self) -> dict[str, object]:
        return {
            "runtime_id": self.runtime_id,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "output_files": list(self.output_files),
        }


class SkillRuntimeBackend(Protocol):
    def run_workflow(
        self,
        workflow_prompt: str,
        *,
        skill_space_ids: list[str],
        session_id: str,
        skill_names: list[str] | None = None,
        env: dict[str, str] | None = None,
        input_files: list[SandboxInputFile] | None = None,
        timeout: int = 900,
    ) -> SkillRuntimeResult:
        ...


def parse_output_files(stdout: str) -> list[str]:
    for line in stdout.splitlines():
        if not line.startswith("workflow_result="):
            continue
        raw = line.split("=", 1)[1]
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return []
        output_files = payload.get("output_files") if isinstance(payload, dict) else None
        if isinstance(output_files, list):
            return [str(item) for item in output_files]
    return []


def normalize_skill_names(skill_names: Sequence[str] | str | None) -> list[str]:
    if skill_names is None:
        return []

    raw_values: list[str]
    if isinstance(skill_names, str):
        raw_values = [skill_names]
    else:
        raw_values = [str(item) for item in skill_names]

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        for part in raw.split(","):
            name = part.strip()
            key = name.lower()
            if not name or key in seen:
                continue
            seen.add(key)
            normalized.append(name)
    return normalized


def format_skill_names_env(skill_names: Sequence[str] | str | None) -> str:
    return ",".join(normalize_skill_names(skill_names))
