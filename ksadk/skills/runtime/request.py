from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ksadk.skills.package_store import SkillPackageError
from ksadk.skills.runtime.base import normalize_skill_names
from ksadk.skills.runtime.pinned import PinnedSkillArchive, validate_package_set


class SkillWorkflowRequestError(ValueError):
    pass


@dataclass(frozen=True)
class SkillWorkflowRequest:
    workflow_prompt: str = ""
    skill_names: list[str] = field(default_factory=list)
    pinned_packages: tuple[PinnedSkillArchive, ...] | None = None
    package_directory: Path | None = None
    collect_artifacts: bool = False


def parse_workflow_request(argv: list[str]) -> SkillWorkflowRequest:
    args = list(argv)
    has_prompt_file = "--prompt-file" in args
    has_request_file = "--request-file" in args
    if has_prompt_file and has_request_file:
        raise SkillWorkflowRequestError("--prompt-file and --request-file cannot be used together")

    if has_request_file:
        return _request_from_json_file(_option_value(args, "--request-file"))
    if has_prompt_file:
        return SkillWorkflowRequest(
            workflow_prompt=Path(_option_value(args, "--prompt-file")).read_text(encoding="utf-8")
        )
    if not args:
        return SkillWorkflowRequest()
    return SkillWorkflowRequest(workflow_prompt=args[0])


def _option_value(args: list[str], name: str) -> str:
    try:
        index = args.index(name)
        value = args[index + 1]
    except (ValueError, IndexError) as exc:
        raise SkillWorkflowRequestError(f"{name} requires a file path") from exc
    if value.startswith("--"):
        raise SkillWorkflowRequestError(f"{name} requires a file path")
    return value


def _request_from_json_file(path: str) -> SkillWorkflowRequest:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SkillWorkflowRequestError(f"invalid request file JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SkillWorkflowRequestError("request file must contain a JSON object")
    prompt = payload.get("workflow_prompt", payload.get("prompt", ""))
    skill_names = _skill_names_from_payload(payload)
    pinned_packages = None
    if "pinned_protocol_version" in payload and "pinned_packages" not in payload:
        raise SkillWorkflowRequestError("Pinned Skill manifest is missing")
    if "pinned_packages" in payload:
        version = payload.get("pinned_protocol_version")
        if type(version) is not int or version != 1:
            raise SkillWorkflowRequestError("Unsupported pinned Skill protocol version")
        raw = payload["pinned_packages"]
        if not isinstance(raw, list):
            raise SkillWorkflowRequestError("pinned_packages must be an array")
        try:
            pinned_packages = tuple(PinnedSkillArchive.model_validate(item) for item in raw)
            validate_package_set(pinned_packages)
        except (ValidationError, SkillPackageError):
            raise SkillWorkflowRequestError("Invalid pinned Skill manifest") from None
        allowed = {entry.name.casefold() for entry in pinned_packages}
        if any(name.casefold() not in allowed for name in skill_names):
            raise SkillWorkflowRequestError("Selected Skill is not in the pinned manifest")
    collect_artifacts = payload.get("collect_artifacts", False)
    if type(collect_artifacts) is not bool or (collect_artifacts and pinned_packages is None):
        raise SkillWorkflowRequestError("Artifact collection requires a pinned request")
    return SkillWorkflowRequest(
        workflow_prompt=str(prompt or ""),
        skill_names=skill_names,
        pinned_packages=pinned_packages,
        package_directory=Path(path).resolve().parent if pinned_packages is not None else None,
        collect_artifacts=collect_artifacts,
    )


def _skill_names_from_payload(payload: dict[str, Any]) -> list[str]:
    for key in ("skill_names", "selected_skills"):
        if key in payload:
            return normalize_skill_names(payload.get(key))
    return []
