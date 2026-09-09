from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence

from ksadk.skills.events import SkillEvent, SkillInvocationPlan
from ksadk.skills.package_store import SkillPackage

_SANDBOX_OBSERVABILITY_ENV_PREFIXES = ("OTEL_", "LANGFUSE_")
_SANDBOX_TRACE_ENV_NAMES = {"BAGGAGE", "TRACEPARENT", "TRACESTATE"}


class SkillRuntimeError(RuntimeError):
    pass


def sandbox_runtime_env(env: dict[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in env.items()
        if not name.startswith(_SANDBOX_OBSERVABILITY_ENV_PREFIXES)
        and name not in _SANDBOX_TRACE_ENV_NAMES
    }


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
    output_text: str = ""
    output_text_truncated: bool = False
    skill_events: list[SkillEvent] = field(default_factory=list)
    workflow_status: str = ""
    executed_skill: str = ""
    instructions: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.error_type and not self.timed_out

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "runtime_id": self.runtime_id,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "output_files": list(self.output_files),
            "workflow_status": self.workflow_status,
            "executed_skill": self.executed_skill,
            "instructions": self.instructions,
        }
        if self.output_text:
            result["output_text"] = self.output_text
        if self.output_text_truncated:
            result["output_text_truncated"] = True
        if self.skill_events:
            result["skill_events"] = [event.to_dict() for event in self.skill_events]
        return result


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
        invocation_plan: SkillInvocationPlan | None = None,
        pinned_packages: list[SkillPackage] | None = None,
        timeout: int = 900,
    ) -> SkillRuntimeResult: ...


@dataclass(frozen=True)
class ParsedWorkflowResult:
    output_files: tuple[str, ...] = ()
    output_text: str = ""
    output_text_truncated: bool = False
    workflow_status: str = ""
    executed_skill: str = ""
    instructions: str = ""


def parse_workflow_result(stdout: str) -> ParsedWorkflowResult:
    """Parse the workflow_result= JSON line from agent.py stdout."""
    for line in stdout.splitlines():
        if not line.startswith("workflow_result="):
            continue
        raw = line.split("=", 1)[1]
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return ParsedWorkflowResult()
        if not isinstance(payload, dict):
            return ParsedWorkflowResult()
        output_files = payload.get("output_files")
        return ParsedWorkflowResult(
            output_files=(
                tuple(str(item) for item in output_files) if isinstance(output_files, list) else ()
            ),
            output_text=payload.get("output_text") if isinstance(payload.get("output_text"), str) else "",
            output_text_truncated=payload.get("output_text_truncated") is True,
            workflow_status=str(payload.get("status") or ""),
            executed_skill=str(payload.get("executed_skill") or ""),
            instructions=str(payload.get("instructions") or ""),
        )
    return ParsedWorkflowResult()


def parse_output_files(stdout: str) -> list[str]:
    """Compatibility helper for callers that only need artifact paths."""
    return list(parse_workflow_result(stdout).output_files)


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
