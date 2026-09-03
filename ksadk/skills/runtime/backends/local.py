from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from ksadk.skills.events import (
    SKILL_EVENT_FILE_ENV,
    SkillEvent,
    SkillInvocationPlan,
    read_sandbox_skill_events,
)
from ksadk.skills.runtime.base import (
    SandboxInputFile,
    SkillRuntimeResult,
    format_skill_names_env,
    normalize_skill_names,
    parse_workflow_result,
    sandbox_runtime_env,
)


def _coerce_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


class LocalProcessSkillRuntimeBackend:
    def __init__(self, agent_path: str | Path, timeout: int = 900):
        self.agent_path = Path(agent_path)
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "LocalProcessSkillRuntimeBackend":
        agent_path = os.environ.get("KSADK_SKILL_RUNTIME_AGENT_PATH") or str(
            Path(__file__).resolve().parents[1] / "agent.py"
        )
        timeout = int(os.environ.get("KSADK_SKILL_RUNTIME_TIMEOUT", "900"))
        return cls(agent_path=agent_path, timeout=timeout)

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
        timeout: int = 900,
    ) -> SkillRuntimeResult:
        started = time.monotonic()
        runtime_id = f"local:{session_id}"
        skill_events: list[SkillEvent] = []
        runtime_env = os.environ.copy()
        runtime_env.update(env or {})
        runtime_env = sandbox_runtime_env(runtime_env)
        runtime_env["KSADK_SKILL_SPACE_IDS"] = ",".join(skill_space_ids)
        runtime_env["SKILL_SPACE_ID"] = skill_space_ids[0] if skill_space_ids else ""
        if public_spaces := os.environ.get("KSADK_PUBLIC_SKILL_SPACE_IDS"):
            runtime_env["KSADK_PUBLIC_SKILL_SPACE_IDS"] = public_spaces
        selected_skill_names = format_skill_names_env(skill_names)
        if selected_skill_names:
            runtime_env["KSADK_SELECTED_SKILL_NAMES"] = selected_skill_names
        else:
            runtime_env.pop("KSADK_SELECTED_SKILL_NAMES", None)
        try:
            with tempfile.TemporaryDirectory(prefix="ksadk-skill-runtime-") as tmp_dir:
                skill_events.append(
                    SkillEvent.create(
                        "sandbox.session.created", status="completed", runtime_id=runtime_id
                    )
                )
                request_path = Path(tmp_dir) / "workflow-request.json"
                event_path = Path(tmp_dir) / "skill-events.jsonl"
                runtime_env[SKILL_EVENT_FILE_ENV] = str(event_path)
                request_payload = {
                    "workflow_prompt": workflow_prompt,
                    "skill_names": normalize_skill_names(skill_names),
                }
                if invocation_plan is not None:
                    request_payload["invocation_plan"] = _request_invocation_plan(invocation_plan)
                request_path.write_text(
                    json.dumps(request_payload, ensure_ascii=False),
                    encoding="utf-8",
                )
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-u",
                        str(self.agent_path),
                        "--request-file",
                        str(request_path),
                    ],
                    text=True,
                    capture_output=True,
                    timeout=timeout or self.timeout,
                    env=runtime_env,
                    check=False,
                )
                workflow_result = parse_workflow_result(completed.stdout)
                skill_events.extend(
                    replace(event, runtime_id=event.runtime_id or runtime_id)
                    for event in read_sandbox_skill_events(
                        event_path, expected_invocations=_expected_invocations(invocation_plan)
                    )
                )
            skill_events.append(
                SkillEvent.create(
                    "sandbox.session.cleaned_up", status="completed", runtime_id=runtime_id
                )
            )
            return SkillRuntimeResult(
                runtime_id=runtime_id,
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                duration_ms=int((time.monotonic() - started) * 1000),
                output_files=list(workflow_result.output_files),
                output_text=workflow_result.output_text,
                output_text_truncated=workflow_result.output_text_truncated,
                skill_events=skill_events,
            )
        except subprocess.TimeoutExpired as exc:
            if skill_events:
                skill_events.append(
                    SkillEvent.create(
                        "sandbox.session.cleaned_up", status="completed", runtime_id=runtime_id
                    )
                )
            return SkillRuntimeResult(
                runtime_id=runtime_id,
                exit_code=None,
                stdout=_coerce_output(exc.stdout),
                stderr=_coerce_output(exc.stderr),
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=True,
                error_type="TimeoutExpired",
                error_message=f"Skill workflow timed out after {timeout or self.timeout}s",
                skill_events=skill_events,
            )


def _request_invocation_plan(plan: SkillInvocationPlan | None) -> list[dict[str, str]]:
    if plan is None:
        return []
    return [
        {
            "skill_id": entry.skill_ref.skill_id,
            "skill_invocation_id": entry.skill_invocation_id,
        }
        for entry in plan.entries
    ]


def _expected_invocations(plan: SkillInvocationPlan | None) -> dict[str, object] | None:
    if plan is None:
        return None
    return {entry.skill_invocation_id: entry.skill_ref for entry in plan.entries}
