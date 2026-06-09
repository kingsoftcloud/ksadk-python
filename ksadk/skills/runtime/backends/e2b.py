from __future__ import annotations

import json
import os
import time
from typing import Any

from ksadk.sandbox import (
    E2BSandboxBackend,
    SandboxInputFile as RuntimeSandboxInputFile,
    SandboxSpec,
)
from ksadk.skills.runtime.base import (
    SandboxInputFile,
    SkillRuntimeError,
    SkillRuntimeResult,
    format_skill_names_env,
    normalize_skill_names,
    parse_output_files,
)


def _bool_env(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _redact(value: str) -> str:
    redacted = value
    for key_name in (
        "E2B_API_KEY",
        "KSADK_SKILL_SERVICE_TOKEN",
        "KSADK_SKILL_SERVICE_ACCESS_KEY",
        "KSADK_SKILL_SERVICE_SECRET_KEY",
        "KSYUN_ACCESS_KEY",
        "KSYUN_SECRET_KEY",
        "KS3_ACCESS_KEY",
        "KS3_SECRET_KEY",
    ):
        secret = os.environ.get(key_name)
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


class E2BSkillRuntimeBackend:
    def __init__(
        self,
        *,
        sandbox_cls: Any | None = None,
        template_id: str,
        timeout: int = 900,
        allow_internet_access: bool = True,
    ):
        if not template_id:
            raise SkillRuntimeError(
                "KSADK_SANDBOX_TEMPLATE_ID is required for E2B backend "
                "(KSADK_SKILL_RUNTIME_TEMPLATE_ID is a compatibility alias)"
            )
        self.template_id = template_id
        self.timeout = timeout
        self.allow_internet_access = allow_internet_access
        self.sandbox_backend = E2BSandboxBackend(
            spec=SandboxSpec(
                template_id=template_id,
                timeout=timeout,
                allow_internet_access=allow_internet_access,
                metadata={"component": "skill-runtime"},
            ),
            sandbox_cls=sandbox_cls,
        )

    @classmethod
    def from_env(cls) -> "E2BSkillRuntimeBackend":
        try:
            from e2b import Sandbox
        except ImportError as exc:
            raise SkillRuntimeError(
                "e2b>=2.0.0 is required for KSADK_SKILL_RUNTIME_BACKEND=e2b"
            ) from exc
        return cls(
            sandbox_cls=Sandbox,
            template_id=(
                os.environ.get("KSADK_SANDBOX_TEMPLATE_ID")
                or os.environ.get("KSADK_SKILL_RUNTIME_TEMPLATE_ID")
                or ""
            ),
            timeout=int(os.environ.get("KSADK_SANDBOX_TIMEOUT") or os.environ.get("KSADK_SKILL_RUNTIME_TIMEOUT") or "900"),
            allow_internet_access=_bool_env(
                "KSADK_SANDBOX_ALLOW_INTERNET_ACCESS",
                _bool_env("KSADK_SKILL_RUNTIME_ALLOW_INTERNET_ACCESS", True),
            ),
        )

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
        session = None
        started = time.monotonic()
        effective_timeout = timeout or self.timeout
        try:
            sandbox_env = {
                "KSADK_SKILL_SPACE_IDS": ",".join(skill_space_ids),
                "SKILL_SPACE_ID": skill_space_ids[0] if skill_space_ids else "",
            }
            if public_spaces := os.environ.get("KSADK_PUBLIC_SKILL_SPACE_IDS"):
                sandbox_env["KSADK_PUBLIC_SKILL_SPACE_IDS"] = public_spaces
            selected_skill_names = format_skill_names_env(skill_names)
            if selected_skill_names:
                sandbox_env["KSADK_SELECTED_SKILL_NAMES"] = selected_skill_names
            sandbox_env.update(env or {})
            session = self.sandbox_backend.create_session(
                session_id=session_id,
                env=sandbox_env,
                input_files=[
                    RuntimeSandboxInputFile(source=item.source, target_path=item.target_path)
                    for item in input_files or []
                ],
            )

            request_path = "/tmp/ksadk-workflow-request.json"
            session.write_file(
                request_path,
                json.dumps(
                    {
                        "workflow_prompt": workflow_prompt,
                        "skill_names": normalize_skill_names(skill_names),
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
            )
            command = f"python -u /home/ksadk/agent.py --request-file {request_path}"
            result = session.run_command(command, timeout=effective_timeout, env=sandbox_env)
            stdout = result.stdout
            return SkillRuntimeResult(
                runtime_id=session.sandbox_id,
                exit_code=result.exit_code,
                stdout=stdout,
                stderr=result.stderr,
                duration_ms=int((time.monotonic() - started) * 1000),
                output_files=parse_output_files(stdout),
            )
        except Exception as exc:
            error_type = type(exc).__name__
            return SkillRuntimeResult(
                runtime_id=session.sandbox_id if session is not None else "",
                exit_code=None,
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out="timeout" in error_type.lower(),
                error_type=error_type,
                error_message=_redact(str(exc)),
            )
        finally:
            if session is not None:
                try:
                    session.kill()
                except Exception:
                    pass
