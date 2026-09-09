from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

from ksadk.sandbox import (
    E2BSandboxBackend,
    SandboxSpec,
)
from ksadk.sandbox import (
    SandboxInputFile as RuntimeSandboxInputFile,
)
from ksadk.sandbox.e2b_connection import ExplicitE2BConnection
from ksadk.skills.package_store import SkillPackage
from ksadk.skills.runtime.artifact_delivery import ArtifactBundle, import_artifacts
from ksadk.skills.runtime.base import (
    SandboxInputFile,
    SkillRuntimeError,
    SkillRuntimeResult,
    format_skill_names_env,
    normalize_skill_names,
    parse_output_files,
)
from ksadk.skills.runtime.pinned import read_archive, stage_packages


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
    def preflight(self) -> None:
        """Read-only local dependency check; does not prove upstream authorization."""
        self.sandbox_backend.check_available()

    def __init__(
        self,
        *,
        sandbox_cls: Any | None = None,
        template_id: str,
        timeout: int = 900,
        allow_internet_access: bool = True,
        connection: ExplicitE2BConnection | None = None,
        artifact_directory: Path | None = None,
    ):
        if not template_id:
            raise SkillRuntimeError(
                "KSADK_SANDBOX_TEMPLATE_ID is required for E2B backend "
                "(KSADK_SKILL_RUNTIME_TEMPLATE_ID is a compatibility alias)"
            )
        self.template_id = template_id
        self.timeout = timeout
        self.allow_internet_access = allow_internet_access
        self.artifact_directory = artifact_directory
        self.sandbox_backend = E2BSandboxBackend(
            spec=SandboxSpec(
                template_id=template_id,
                timeout=timeout,
                allow_internet_access=allow_internet_access,
                metadata={"component": "skill-runtime"},
            ),
            sandbox_cls=sandbox_cls,
            connection=connection,
        )

    @classmethod
    def from_env(cls) -> "E2BSkillRuntimeBackend":
        try:
            from e2b import Sandbox  # type: ignore[import-not-found, import-untyped]
        except ImportError as exc:
            raise SkillRuntimeError(
                "e2b>=2.15.3,<2.25.0 is required for KSADK_SKILL_RUNTIME_BACKEND=e2b"
            ) from exc
        return cls(
            sandbox_cls=Sandbox,
            template_id=(
                os.environ.get("KSADK_SANDBOX_TEMPLATE_ID")
                or os.environ.get("KSADK_SKILL_RUNTIME_TEMPLATE_ID")
                or ""
            ),
            timeout=int(
                os.environ.get("KSADK_SANDBOX_TIMEOUT")
                or os.environ.get("KSADK_SKILL_RUNTIME_TIMEOUT")
                or "900"
            ),
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
        pinned_packages: list[SkillPackage] | None = None,
        timeout: int = 900,
    ) -> SkillRuntimeResult:
        session = None
        started = time.monotonic()
        effective_timeout = timeout or self.timeout
        try:
            if pinned_packages is not None:
                for item in input_files or []:
                    target = PurePosixPath(item.target_path)
                    if (
                        not target.is_relative_to("/workspace/inputs")
                        or ".." in target.parts
                        or "\\" in item.target_path
                    ):
                        raise SkillRuntimeError(
                            "Pinned Skill inputs must be under /workspace/inputs"
                        )
            sandbox_env = {
                "KSADK_SKILL_SPACE_IDS": ",".join(skill_space_ids),
                "SKILL_SPACE_ID": skill_space_ids[0] if skill_space_ids else "",
            }
            if pinned_packages is None and (
                public_spaces := os.environ.get("KSADK_PUBLIC_SKILL_SPACE_IDS")
            ):
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
            request = {
                "workflow_prompt": workflow_prompt,
                "skill_names": normalize_skill_names(skill_names),
            }
            if pinned_packages is not None:
                probe = session.run_command(
                    "python -I -c 'from ksadk.skills.runtime.agent import "
                    "PINNED_PACKAGE_PROTOCOL_VERSION, ARTIFACT_DELIVERY_PROTOCOL_VERSION; "
                    'print(f"{PINNED_PACKAGE_PROTOCOL_VERSION}:{ARTIFACT_DELIVERY_PROTOCOL_VERSION}")\'',
                    timeout=min(effective_timeout, 10),
                    env=sandbox_env,
                )
                if probe.exit_code != 0 or probe.stdout.strip() != "1:1":
                    raise SkillRuntimeError(
                        "Sandbox runtime does not support pinned Skill protocol v1"
                    )
                delivery = "/tmp/ksadk-pinned-" + secrets.token_hex(16)
                prepared = session.run_command(
                    f"mkdir -m 700 {delivery}", timeout=min(effective_timeout, 10), env=sandbox_env
                )
                if prepared.exit_code != 0:
                    raise SkillRuntimeError("Could not prepare pinned Skill delivery directory")
                with tempfile.TemporaryDirectory(prefix="ksadk-skill-transfer-") as directory:
                    entries = stage_packages(pinned_packages, Path(directory))
                    for entry in entries:
                        session.write_file(
                            f"{delivery}/{entry.archive_name}",
                            read_archive(Path(directory) / entry.archive_name),
                        )
                request["pinned_packages"] = [entry.model_dump() for entry in entries]
                request["pinned_protocol_version"] = 1
                request["collect_artifacts"] = True
                sandbox_env["KSADK_SKILL_WORKDIR"] = f"{delivery}/work"
                request_path = f"{delivery}/workflow-request.json"
            session.write_file(
                request_path,
                json.dumps(
                    request,
                    ensure_ascii=False,
                ).encode("utf-8"),
            )
            command = f"python -u /home/ksadk/agent.py --request-file {request_path}"
            if pinned_packages is not None:
                command = (
                    f"python -I -u -m ksadk.skills.runtime.agent --request-file {request_path}"
                )
            result = session.run_command(command, timeout=effective_timeout, env=sandbox_env)
            stdout = result.stdout
            output_files = parse_output_files(stdout)
            if pinned_packages is not None:
                payloads = [
                    json.loads(line.split("=", 1)[1])
                    for line in stdout.splitlines()
                    if line.startswith("workflow_result=")
                ]
                if len(payloads) != 1 or not isinstance(payloads[0], dict):
                    raise SkillRuntimeError("Sandbox did not return a unique workflow result")
                payload = payloads[0]
                if payload.get("artifact_bundle") is None:
                    raise SkillRuntimeError("Sandbox did not return an artifact delivery receipt")
                receipt = ArtifactBundle.model_validate(payload["artifact_bundle"])
                content = session.read_file_bytes(
                    f"{delivery}/artifacts.zip", max_bytes=receipt.size
                )
                output_files = import_artifacts(content, receipt, parent=self.artifact_directory)
                payload["output_files"] = output_files
                payload["artifacts"] = output_files
                stdout = (
                    "\n".join(
                        "workflow_result=" + json.dumps(payload, ensure_ascii=False, sort_keys=True)
                        if line.startswith("workflow_result=")
                        else line
                        for line in stdout.splitlines()
                    )
                    + "\n"
                )
            return SkillRuntimeResult(
                runtime_id=session.sandbox_id,
                exit_code=result.exit_code,
                stdout=stdout,
                stderr=result.stderr,
                duration_ms=int((time.monotonic() - started) * 1000),
                output_files=output_files,
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
