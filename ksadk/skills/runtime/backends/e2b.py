from __future__ import annotations

import json
import logging
import os
import secrets
import shlex
import tempfile
import time
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from ksadk.sandbox import (
    E2BSandboxBackend,
    SandboxSpec,
)
from ksadk.sandbox import (
    SandboxInputFile as RuntimeSandboxInputFile,
)
from ksadk.sandbox.backends.e2b import E2BSandboxSessionError
from ksadk.sandbox.diagnostics import e2b_diagnostic_step
from ksadk.sandbox.e2b_connection import ExplicitE2BConnection
from ksadk.skills.events import (
    SKILL_EVENT_FILE_ENV,
    SkillEvent,
    SkillInvocationPlan,
    parse_sandbox_skill_event_lines,
)
from ksadk.skills.package_store import SkillPackage
from ksadk.skills.runtime.artifact_delivery import ArtifactBundle, import_artifacts
from ksadk.skills.runtime.base import (
    SandboxInputFile,
    SkillRuntimeError,
    SkillRuntimeResult,
    format_skill_names_env,
    normalize_skill_names,
    parse_workflow_result,
    sandbox_runtime_env,
)
from ksadk.skills.runtime.legacy_local import (
    LEGACY_LOCAL_PROTOCOL,
    PINNED_PROTOCOL,
    LegacyDelivery,
    collect_legacy_artifacts,
    legacy_environment,
    prepare_legacy_delivery,
    resolve_protocol,
    rewrite_workflow_artifacts,
)
from ksadk.skills.runtime.pinned import read_archive, stage_packages

logger = logging.getLogger(__name__)

_SANDBOX_VERSION_FIELDS = frozenset(
    {
        "python_version",
        "python_executable",
        "ksadk_version",
        "ksadk_file",
        "agent_file",
        "agent_sha256",
        "pinned_protocol",
        "artifact_protocol",
        "metadata_error",
        "ksadk_import_error",
        "agent_import_error",
    }
)

# Run with the same isolated Python interpreter as the pinned protocol check.
# Import failures are data so old/incomplete installations can still report versions.
_SANDBOX_VERSION_PROBE = r"""
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import sys
from pathlib import Path
report = {"python_version": sys.version.split()[0], "python_executable": sys.executable}
try:
    report["ksadk_version"] = importlib.metadata.version("ksadk")
except Exception as exc:
    report["metadata_error"] = type(exc).__name__
try:
    ksadk = importlib.import_module("ksadk")
    report["ksadk_file"] = getattr(ksadk, "__file__", None)
except Exception as exc:
    report["ksadk_import_error"] = type(exc).__name__
try:
    spec = importlib.util.find_spec("ksadk.skills.runtime.agent")
    report["agent_file"] = spec.origin if spec else None
    if spec and spec.origin and Path(spec.origin).is_file():
        report["agent_sha256"] = hashlib.sha256(Path(spec.origin).read_bytes()).hexdigest()
    agent = importlib.import_module("ksadk.skills.runtime.agent")
    report["pinned_protocol"] = getattr(agent, "PINNED_PACKAGE_PROTOCOL_VERSION", None)
    report["artifact_protocol"] = getattr(agent, "ARTIFACT_DELIVERY_PROTOCOL_VERSION", None)
except Exception as exc:
    report["agent_import_error"] = type(exc).__name__
print(json.dumps(report))
"""


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
    @staticmethod
    def _log_sandbox_version(session, env, sensitive_values) -> None:
        # Diagnostic failure must not replace the original protocol failure.
        try:
            with e2b_diagnostic_step(
                "pinned.version_probe",
                runtime_id=session.sandbox_id,
                env=env,
                sensitive_values=sensitive_values,
            ):
                result = session.run_command(
                    "python -I -c " + shlex.quote(_SANDBOX_VERSION_PROBE),
                    timeout=10,
                    env=env,
                )
                if result.exit_code != 0:
                    raise SkillRuntimeError("Sandbox version diagnostic command failed")
                report = json.loads(result.stdout)
                if not isinstance(report, dict):
                    raise SkillRuntimeError("Invalid sandbox version diagnostic report")
                # Whitelist fields; never log arbitrary remote stdout or environment values.
                report = {
                    key: value
                    for key, value in report.items()
                    if key in _SANDBOX_VERSION_FIELDS
                    and (value is None or type(value) in (str, int, bool))
                }
                detail = _redact(json.dumps(report, ensure_ascii=True))
                for value in sorted(
                    set(sensitive_values + tuple(env.values())), key=len, reverse=True
                ):
                    if value:
                        detail = detail.replace(value, "[REDACTED]")
                logger.warning(
                    "E2B_DIAGNOSTIC step=pinned.version_probe status=report "
                    "runtime_id=%s details=%s",
                    session.sandbox_id,
                    detail,
                )
        except Exception:
            pass

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
        invocation_plan: SkillInvocationPlan | None = None,
        pinned_packages: list[SkillPackage] | None = None,
        timeout: int = 900,
    ) -> SkillRuntimeResult:
        session = None
        connection = getattr(self.sandbox_backend, "connection", None)
        diagnostic_secrets = (connection.api_key.get_secret_value(),) if connection else ()
        started = time.monotonic()
        effective_timeout = timeout or self.timeout
        skill_events: list[SkillEvent] = []
        runtime_result: SkillRuntimeResult | None = None
        protocol = ""
        legacy_delivery: LegacyDelivery | None = None
        legacy_collection_attempted = False
        workflow_started = False
        event_path = ""
        stdout_path = ""
        stderr_path = ""
        failure_stage = "create"
        sandbox: dict[str, object] = {
            "backend": "e2b",
            "runtime_id": "",
            "creation_status": "failed",
            "instance_status": "not_created",
            "cleanup_status": "not_started",
            "cleanup_error": None,
            "cleanup_scope": "sandbox_instance",
        }
        try:
            if pinned_packages is not None:
                protocol = resolve_protocol()
                sandbox["skill_delivery_protocol"] = protocol
                if protocol == LEGACY_LOCAL_PROTOCOL:
                    legacy_root = "/tmp/ksadk-legacy-" + secrets.token_hex(16)
                    legacy_delivery = LegacyDelivery(
                        root=legacy_root,
                        skills_dir=f"{legacy_root}/skills",
                        work_dir=f"{legacy_root}/work",
                        request_path=f"{legacy_root}/workflow-request.json",
                        collector_path=f"{legacy_root}/collect-artifacts.py",
                        artifact_manifest_path=f"{legacy_root}/artifact-paths.json",
                        artifact_bundle_path=f"{legacy_root}/artifacts.zip",
                    )
                    sandbox.update(
                        artifact_collection_status="not_started",
                        artifact_collection_source=None,
                        artifact_collection_error=None,
                    )
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
            sandbox_env = sandbox_runtime_env(sandbox_env)
            if legacy_delivery is not None:
                # Apply after caller env so inherited/bespoke discovery settings cannot
                # make the old sandbox fetch a different package from Skill Service.
                sandbox_env.update(legacy_environment(legacy_delivery))
            session = self.sandbox_backend.create_session(
                session_id=session_id,
                env=sandbox_env,
                input_files=[
                    RuntimeSandboxInputFile(source=item.source, target_path=item.target_path)
                    for item in input_files or []
                ],
            )
            sandbox.update(
                runtime_id=session.sandbox_id,
                creation_status="completed",
                instance_status="created",
            )
            failure_stage = "initialize"
            skill_events.append(
                SkillEvent.create(
                    "sandbox.session.created",
                    status="completed",
                    runtime_id=session.sandbox_id,
                )
            )

            request_path = (
                legacy_delivery.request_path
                if legacy_delivery is not None
                else "/tmp/ksadk-workflow-request.json"
            )
            request = {
                "workflow_prompt": workflow_prompt,
                "skill_names": normalize_skill_names(skill_names),
            }
            if invocation_plan is not None and legacy_delivery is None:
                request["invocation_plan"] = [
                    {
                        "skill_id": entry.skill_ref.skill_id,
                        "skill_invocation_id": entry.skill_invocation_id,
                    }
                    for entry in invocation_plan.entries
                ]
            if pinned_packages is not None and protocol == PINNED_PROTOCOL:
                try:
                    with e2b_diagnostic_step(
                        "pinned.protocol_probe",
                        runtime_id=session.sandbox_id,
                        env=sandbox_env,
                        sensitive_values=diagnostic_secrets,
                    ):
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
                except Exception:
                    self._log_sandbox_version(session, sandbox_env, diagnostic_secrets)
                    raise
                delivery = "/tmp/ksadk-pinned-" + secrets.token_hex(16)
                with e2b_diagnostic_step(
                    "pinned.prepare_directory",
                    runtime_id=session.sandbox_id,
                    env=sandbox_env,
                    sensitive_values=diagnostic_secrets,
                ):
                    prepared = session.run_command(
                        f"mkdir -m 700 {delivery}",
                        timeout=min(effective_timeout, 10),
                        env=sandbox_env,
                    )
                    if prepared.exit_code != 0:
                        raise SkillRuntimeError("Could not prepare pinned Skill delivery directory")
                with e2b_diagnostic_step(
                    "pinned.package_upload",
                    runtime_id=session.sandbox_id,
                    env=sandbox_env,
                    sensitive_values=diagnostic_secrets,
                ):
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
            elif pinned_packages is not None and legacy_delivery is not None:
                with e2b_diagnostic_step(
                    "legacy_local.protocol_probe",
                    runtime_id=session.sandbox_id,
                    env=sandbox_env,
                    sensitive_values=diagnostic_secrets,
                ):
                    probe = session.run_command(
                        "python -I -c "
                        + shlex.quote(
                            "import importlib.metadata; "
                            "from ksadk.skills.runtime.loader import load_local_skills; "
                            "from ksadk.skills.runtime.request import parse_workflow_request; "
                            "print(importlib.metadata.version('ksadk'))"
                        ),
                        timeout=min(effective_timeout, 10),
                        env=sandbox_env,
                    )
                    if probe.exit_code != 0 or probe.stdout.strip() != "0.8.2":
                        raise SkillRuntimeError(
                            "Sandbox runtime does not match legacy local KsADK 0.8.2"
                        )
                with e2b_diagnostic_step(
                    "legacy_local.package_upload",
                    runtime_id=session.sandbox_id,
                    env=sandbox_env,
                    sensitive_values=diagnostic_secrets,
                ):
                    prepare_legacy_delivery(
                        session,
                        pinned_packages,
                        delivery=legacy_delivery,
                        timeout=min(effective_timeout, 30),
                        env=sandbox_env,
                    )
            with e2b_diagnostic_step(
                "workflow.request_write",
                runtime_id=session.sandbox_id,
                env=sandbox_env,
                sensitive_values=diagnostic_secrets,
            ):
                session.write_file(
                    request_path,
                    json.dumps(request, ensure_ascii=False).encode("utf-8"),
                )
            event_path = f"/tmp/ksadk-skill-events-{uuid4().hex}.jsonl"
            command_env = {**sandbox_env, SKILL_EVENT_FILE_ENV: event_path}
            command = f"python -u /home/ksadk/agent.py --request-file {request_path}"
            if pinned_packages is not None:
                command = (
                    f"python -I -u -m ksadk.skills.runtime.agent --request-file {request_path}"
                )
            output_prefix = f"/tmp/ksadk-workflow-output-{uuid4().hex}"
            stdout_path = output_prefix + ".stdout"
            stderr_path = output_prefix + ".stderr"
            failure_stage = "execute"
            workflow_started = True
            result = session.run_command(
                f"{command} > {stdout_path} 2> {stderr_path}",
                timeout=effective_timeout,
                env=command_env,
            )
            stdout = _read_output(session, stdout_path, result.stdout)
            stderr = _read_output(session, stderr_path, result.stderr)
            output_files = list(parse_workflow_result(stdout).output_files)
            workflow_result = parse_workflow_result(stdout)
            runtime_result = SkillRuntimeResult(
                runtime_id=session.sandbox_id,
                exit_code=result.exit_code,
                stdout=stdout,
                stderr=stderr,
                duration_ms=int((time.monotonic() - started) * 1000),
                # Legacy paths still point into the sandbox and are untrusted until
                # the bounded collector publishes verified host files below.
                output_files=[] if legacy_delivery is not None else output_files,
                output_text=workflow_result.output_text,
                output_text_truncated=workflow_result.output_text_truncated,
                skill_events=skill_events,
                workflow_status=workflow_result.workflow_status,
                executed_skill=workflow_result.executed_skill,
                instructions=workflow_result.instructions,
            )
            if pinned_packages is not None and protocol == PINNED_PROTOCOL:
                failure_stage = "collect"
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
                        (
                            "workflow_result="
                            + json.dumps(payload, ensure_ascii=False, sort_keys=True)
                            if line.startswith("workflow_result=")
                            else line
                        )
                        for line in stdout.splitlines()
                    )
                    + "\n"
                )
            elif legacy_delivery is not None:
                failure_stage = "collect"
                legacy_collection_attempted = True
                with e2b_diagnostic_step(
                    "legacy_local.artifact_collect",
                    runtime_id=session.sandbox_id,
                    env=sandbox_env,
                    sensitive_values=diagnostic_secrets,
                ):
                    collected = collect_legacy_artifacts(
                        session,
                        stdout=stdout,
                        delivery=legacy_delivery,
                        timeout=min(effective_timeout, 30),
                        env=sandbox_env,
                        parent=self.artifact_directory,
                        recover=result.exit_code not in (0, None),
                    )
                output_files = collected.output_files
                stdout = rewrite_workflow_artifacts(stdout, output_files)
                sandbox.update(
                    artifact_collection_status="completed",
                    artifact_collection_source=collected.source,
                )
            workflow_result = parse_workflow_result(stdout)
            runtime_result = replace(
                runtime_result,
                stdout=stdout,
                output_files=output_files,
                output_text=workflow_result.output_text,
                output_text_truncated=workflow_result.output_text_truncated,
                workflow_status=workflow_result.workflow_status,
                executed_skill=workflow_result.executed_skill,
                instructions=workflow_result.instructions,
            )
            failure_stage = ""
        except Exception as exc:
            reported_exc = exc.cause if isinstance(exc, E2BSandboxSessionError) else exc
            if isinstance(exc, E2BSandboxSessionError):
                sandbox.update(
                    runtime_id=exc.runtime_id,
                    creation_status=("completed" if exc.instance_status == "created" else "failed"),
                    instance_status=exc.instance_status,
                    cleanup_status=exc.cleanup_status,
                    cleanup_error=exc.cleanup_error,
                    failure_stage=exc.failure_stage,
                )
                if exc.runtime_id and exc.cleanup_status in {"completed", "failed"}:
                    cleanup_succeeded = exc.cleanup_status == "completed"
                    skill_events.append(
                        SkillEvent.create(
                            (
                                "sandbox.session.cleaned_up"
                                if cleanup_succeeded
                                else "sandbox.session.cleanup_failed"
                            ),
                            status="completed" if cleanup_succeeded else "failed",
                            runtime_id=exc.runtime_id,
                            error_category="" if cleanup_succeeded else "cleanup_failed",
                        )
                    )
            else:
                sandbox["failure_stage"] = failure_stage
            if legacy_delivery is not None and failure_stage == "collect":
                sandbox.update(
                    artifact_collection_status="failed",
                    artifact_collection_error=type(reported_exc).__name__,
                )
            error_type = type(reported_exc).__name__
            values = {
                "runtime_id": (
                    session.sandbox_id
                    if session is not None
                    else str(sandbox.get("runtime_id") or "")
                ),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "timed_out": "timeout" in error_type.lower(),
                "error_type": error_type,
                "error_message": _redact(str(reported_exc)),
            }
            runtime_result = (
                replace(runtime_result, **values)
                if runtime_result is not None
                else SkillRuntimeResult(
                    exit_code=getattr(reported_exc, "exit_code", None),
                    stdout=_exception_output(reported_exc, "stdout"),
                    stderr=_exception_output(reported_exc, "stderr"),
                    **values,
                )
            )
        finally:
            if session is not None:
                try:
                    if runtime_result is not None and (
                        runtime_result.error_type or runtime_result.timed_out
                    ):
                        recovered_stdout = _read_output(
                            session, stdout_path, runtime_result.stdout
                        )
                        runtime_result = replace(
                            runtime_result,
                            stdout=recovered_stdout,
                            stderr=_read_output(session, stderr_path, runtime_result.stderr),
                            workflow_status=parse_workflow_result(
                                recovered_stdout
                            ).workflow_status,
                            executed_skill=parse_workflow_result(
                                recovered_stdout
                            ).executed_skill,
                        )
                    if (
                        legacy_delivery is not None
                        and workflow_started
                        and not legacy_collection_attempted
                        and runtime_result is not None
                    ):
                        legacy_collection_attempted = True
                        try:
                            with e2b_diagnostic_step(
                                "legacy_local.artifact_recovery",
                                runtime_id=session.sandbox_id,
                                env=sandbox_env,
                                sensitive_values=diagnostic_secrets,
                            ):
                                collected = collect_legacy_artifacts(
                                    session,
                                    stdout=runtime_result.stdout,
                                    delivery=legacy_delivery,
                                    timeout=min(effective_timeout, 30),
                                    env=sandbox_env,
                                    parent=self.artifact_directory,
                                    recover=True,
                                )
                            recovered_stdout = rewrite_workflow_artifacts(
                                runtime_result.stdout, collected.output_files
                            )
                            parsed = parse_workflow_result(recovered_stdout)
                            runtime_result = replace(
                                runtime_result,
                                stdout=recovered_stdout,
                                output_files=collected.output_files,
                                workflow_status=parsed.workflow_status,
                                executed_skill=parsed.executed_skill,
                                instructions=parsed.instructions,
                            )
                            sandbox.update(
                                artifact_collection_status="completed",
                                artifact_collection_source=collected.source,
                            )
                        except Exception as collection_exc:
                            sandbox.update(
                                artifact_collection_status="failed",
                                artifact_collection_error=type(collection_exc).__name__,
                            )
                    if event_path:
                        try:
                            expected_invocations = (
                                {
                                    entry.skill_invocation_id: entry.skill_ref
                                    for entry in invocation_plan.entries
                                }
                                if invocation_plan
                                else None
                            )
                            skill_events.extend(
                                replace(event, runtime_id=event.runtime_id or session.sandbox_id)
                                for event in parse_sandbox_skill_event_lines(
                                    session.read_file(event_path),
                                    expected_invocations=expected_invocations,
                                )
                            )
                        except Exception:
                            pass
                finally:
                    try:
                        with e2b_diagnostic_step(
                            "workflow.cleanup",
                            runtime_id=session.sandbox_id,
                            env=sandbox_env,
                            sensitive_values=diagnostic_secrets,
                        ):
                            session.kill()
                        sandbox["cleanup_status"] = "completed"
                        skill_events.append(
                            SkillEvent.create(
                                "sandbox.session.cleaned_up",
                                status="completed",
                                runtime_id=session.sandbox_id,
                            )
                        )
                    except Exception:
                        sandbox.update(cleanup_status="failed", cleanup_error="cleanup_failed")
                        if not sandbox.get("failure_stage"):
                            sandbox["failure_stage"] = "cleanup"
                        skill_events.append(
                            SkillEvent.create(
                                "sandbox.session.cleanup_failed",
                                status="failed",
                                runtime_id=session.sandbox_id,
                                error_category="cleanup_failed",
                            )
                        )
        assert runtime_result is not None
        return replace(runtime_result, skill_events=skill_events, sandbox=sandbox)


def _read_output(session, path: str, fallback: str) -> str:
    if not path:
        return fallback
    try:
        value = session.read_file(path)
    except Exception:
        return fallback
    return value or fallback


def _exception_output(exc, name: str) -> str:
    value = getattr(exc, name, "")
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""
