from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ksadk.skills.runtime.base import (
    SandboxInputFile,
    SkillRuntimeResult,
    format_skill_names_env,
    parse_output_files,
)


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
        timeout: int = 900,
    ) -> SkillRuntimeResult:
        started = time.monotonic()
        runtime_env = os.environ.copy()
        runtime_env.update(env or {})
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
                prompt_path = Path(tmp_dir) / "workflow-prompt.txt"
                prompt_path.write_text(workflow_prompt, encoding="utf-8")
                completed = subprocess.run(
                    [sys.executable, "-u", str(self.agent_path), "--prompt-file", str(prompt_path)],
                    text=True,
                    capture_output=True,
                    timeout=timeout or self.timeout,
                    env=runtime_env,
                    check=False,
                )
            return SkillRuntimeResult(
                runtime_id=f"local:{session_id}",
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                duration_ms=int((time.monotonic() - started) * 1000),
                output_files=parse_output_files(completed.stdout),
            )
        except subprocess.TimeoutExpired as exc:
            return SkillRuntimeResult(
                runtime_id=f"local:{session_id}",
                exit_code=None,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=True,
                error_type="TimeoutExpired",
                error_message=f"Skill workflow timed out after {timeout or self.timeout}s",
            )
