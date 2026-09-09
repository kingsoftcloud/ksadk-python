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
from ksadk.skills.package_store import SkillPackage
from ksadk.skills.runtime.artifact_delivery import export_artifacts, import_artifacts
from ksadk.skills.runtime.base import (
    SandboxInputFile,
    SkillRuntimeError,
    SkillRuntimeResult,
    format_skill_names_env,
    normalize_skill_names,
    parse_output_files,
    parse_workflow_result,
    sandbox_runtime_env,
)
from ksadk.skills.runtime.pinned import stage_packages


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
        pinned_packages: list[SkillPackage] | None = None,
        timeout: int = 900,
    ) -> SkillRuntimeResult:
        started = time.monotonic()
        runtime_id = f"local:{session_id}"
        skill_events: list[SkillEvent] = []
        if pinned_packages is not None and self.agent_path.resolve() != (
            Path(__file__).resolve().parents[1] / "agent.py"
        ):
            raise ValueError("Pinned Skill execution requires the bundled runtime agent")
        runtime_env = (
            {
                key: value
                for key, value in os.environ.items()
                if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"}
            }
            if pinned_packages is not None
            else os.environ.copy()
        )
        runtime_env.update(env or {})
        runtime_env = sandbox_runtime_env(runtime_env)
        runtime_env["KSADK_SKILL_SPACE_IDS"] = ",".join(skill_space_ids)
        runtime_env["SKILL_SPACE_ID"] = skill_space_ids[0] if skill_space_ids else ""
        if pinned_packages is None and (
            public_spaces := os.environ.get("KSADK_PUBLIC_SKILL_SPACE_IDS")
        ):
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
                if pinned_packages is not None:
                    entries = stage_packages(pinned_packages, Path(tmp_dir))
                    request_payload["pinned_packages"] = [entry.model_dump() for entry in entries]
                    request_payload["pinned_protocol_version"] = 1
                    if not runtime_env.get("KSADK_SKILL_WORKDIR"):
                        # Artifacts outlive the temporary archive delivery directory.
                        runtime_env["KSADK_SKILL_WORKDIR"] = tempfile.mkdtemp(
                            prefix="ksadk-pinned-artifacts-"
                        )
                    # Use the same canonical root in the child and host validator
                    # (macOS temporary directories may be reached through /var).
                    runtime_env["KSADK_SKILL_WORKDIR"] = str(
                        Path(runtime_env["KSADK_SKILL_WORKDIR"]).resolve()
                    )
                request_path.write_text(
                    json.dumps(
                        request_payload,
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                completed = subprocess.run(
                    [
                        sys.executable,
                        *(["-I"] if pinned_packages is not None else []),
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
                stdout = completed.stdout
                output_files = parse_output_files(stdout)
                if pinned_packages is not None:
                    payloads = [
                        json.loads(line.split("=", 1)[1])
                        for line in stdout.splitlines()
                        if line.startswith("workflow_result=")
                    ]
                    if len(payloads) != 1 or not isinstance(payloads[0], dict):
                        raise SkillRuntimeError("Runtime did not return a unique workflow result")
                    payload = payloads[0]
                    paths = payload.get("output_files")
                    if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
                        raise SkillRuntimeError("Runtime returned invalid artifact paths")
                    # Snapshot only bounded regular files from the admitted workspace.
                    # A workflow's stdout is not authority to read arbitrary host files.
                    bundle_path = Path(tmp_dir) / "artifacts.zip"
                    receipt = export_artifacts(
                        paths, Path(runtime_env["KSADK_SKILL_WORKDIR"]), bundle_path
                    )
                    output_files = import_artifacts(bundle_path.read_bytes(), receipt)
                    payload["output_files"] = output_files
                    payload["artifacts"] = output_files
                    payload["artifact_bundle"] = receipt.model_dump()
                    stdout = "\n".join(
                        "workflow_result=" + json.dumps(payload, ensure_ascii=False, sort_keys=True)
                        if line.startswith("workflow_result=")
                        else line
                        for line in stdout.splitlines()
                    ) + "\n"
                workflow_result = parse_workflow_result(stdout)
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
                stdout=stdout,
                stderr=completed.stderr,
                duration_ms=int((time.monotonic() - started) * 1000),
                output_files=output_files,
                output_text=workflow_result.output_text,
                output_text_truncated=workflow_result.output_text_truncated,
                workflow_status=workflow_result.workflow_status,
                executed_skill=workflow_result.executed_skill,
                instructions=workflow_result.instructions,
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
