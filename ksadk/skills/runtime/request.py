from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ksadk.skills.runtime.base import normalize_skill_names


class SkillWorkflowRequestError(ValueError):
    pass


@dataclass(frozen=True)
class SkillWorkflowRequest:
    workflow_prompt: str = ""
    skill_names: list[str] = field(default_factory=list)


def parse_workflow_request(argv: list[str]) -> SkillWorkflowRequest:
    args = list(argv)
    has_prompt_file = "--prompt-file" in args
    has_request_file = "--request-file" in args
    if has_prompt_file and has_request_file:
        raise SkillWorkflowRequestError("--prompt-file and --request-file cannot be used together")

    if has_request_file:
        return _request_from_json_file(_option_value(args, "--request-file"))
    if has_prompt_file:
        return SkillWorkflowRequest(workflow_prompt=Path(_option_value(args, "--prompt-file")).read_text(encoding="utf-8"))
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
    return SkillWorkflowRequest(
        workflow_prompt=str(prompt or ""),
        skill_names=skill_names,
    )


def _skill_names_from_payload(payload: dict[str, Any]) -> list[str]:
    for key in ("skill_names", "selected_skills"):
        if key in payload:
            return normalize_skill_names(payload.get(key))
    return []
