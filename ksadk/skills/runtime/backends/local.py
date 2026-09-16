from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from uuid import uuid4

from ksadk._process import starts_new_process_group, terminate_process_group
from ksadk.sandbox.local_controls import LocalControlSettings
from ksadk.skills.events import (
    SKILL_EVENT_FILE_ENV,
    SkillEvent,
    SkillInvocationPlan,
    parse_sandbox_skill_event_lines,
)
from ksadk.skills.package_store import SkillPackage
from ksadk.skills.runtime.artifact_delivery import (
    MAX_FILES,
    ArtifactDeliveryError,
    export_artifacts,
    import_artifacts,
)
from ksadk.skills.runtime.base import (
    SandboxInputFile,
    SkillRuntimeError,
    SkillRuntimeResult,
    format_skill_names_env,
    normalize_skill_names,
    parse_workflow_result,
    sandbox_runtime_env,
)
from ksadk.skills.runtime.pinned import stage_packages

_MAX_EVENT_FILE_BYTES = 1024 * 1024
_MAX_PROCESS_OUTPUT_BYTES = 4 * 1024 * 1024


class LocalProcessSkillRuntimeBackend:
    """Run the Skill runtime in a host process with request-scoped storage.

    ``artifact_directory`` is a caller-owned parent for verified artifact
    snapshots. When omitted, each non-empty result is delivered to a temporary
    directory that the caller must remove after consuming ``output_files``.
    """

    def __init__(
        self,
        agent_path: str | Path,
        timeout: int = 900,
        artifact_directory: Path | None = None,
        controls: LocalControlSettings | None = None,
    ):
        self.agent_path = Path(agent_path)
        self.timeout = timeout
        self.artifact_directory = artifact_directory
        self.controls = controls or LocalControlSettings.from_trusted_host_env()

    @classmethod
    def from_env(cls) -> "LocalProcessSkillRuntimeBackend":
        agent_path = os.environ.get("KSADK_SKILL_RUNTIME_AGENT_PATH") or str(_bundled_agent_path())
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
        del input_files  # Local Skill input staging is not part of the current contract.
        started = time.monotonic()
        runtime_id = f"local:{session_id}:{uuid4().hex}"
        lifecycle_attributes = {
            "backend": "local_process",
            "cleanup_scope": "request_directory",
        }
        sandbox: dict[str, object] = {
            "backend": "local_process",
            "runtime_id": runtime_id,
            "creation_status": "unavailable",
            "instance_status": "not_created",
            "cleanup_status": "not_started",
            "cleanup_error": None,
            "cleanup_scope": "request_directory",
        }
        skill_events: list[SkillEvent] = []
        request_root: Path | None = None
        event_path: Path | None = None
        process: subprocess.Popen[bytes] | None = None
        result: SkillRuntimeResult | None = None
        primary_error: BaseException | None = None
        primary_traceback: TracebackType | None = None
        process_cleanup_error: str | None = None
        failure_stage = "create"

        try:
            if pinned_packages is not None and self.agent_path.resolve() != _bundled_agent_path():
                raise ValueError("Pinned Skill execution requires the bundled runtime agent")

            request_parent = _request_parent(env)
            request_root = Path(
                tempfile.mkdtemp(prefix="ksadk-skill-request-", dir=request_parent)
            ).resolve()
            work_dir = request_root / "work"
            package_dir = request_root / "packages"
            process_tmp_dir = request_root / "tmp"
            for directory in (work_dir, package_dir, process_tmp_dir):
                directory.mkdir(mode=0o700)

            runtime_env = _runtime_environment(
                env,
                pinned=pinned_packages is not None,
                extra_env_names=self.controls.extra_env_names,
            )
            # Apply controls after caller environment. A Skill invocation cannot
            # disable or weaken the host-selected launch policy.
            runtime_env.update(self.controls.runtime_environment())
            runtime_env.update(
                {
                    "KSADK_SKILL_SPACE_IDS": ",".join(skill_space_ids),
                    "SKILL_SPACE_ID": skill_space_ids[0] if skill_space_ids else "",
                    "KSADK_SKILL_WORKDIR": str(work_dir),
                    "TMPDIR": str(process_tmp_dir),
                }
            )
            if pinned_packages is None and (
                public_spaces := os.environ.get("KSADK_PUBLIC_SKILL_SPACE_IDS")
            ):
                runtime_env["KSADK_PUBLIC_SKILL_SPACE_IDS"] = public_spaces
            selected_skill_names = format_skill_names_env(skill_names)
            if selected_skill_names:
                runtime_env["KSADK_SELECTED_SKILL_NAMES"] = selected_skill_names
            else:
                runtime_env.pop("KSADK_SELECTED_SKILL_NAMES", None)

            request_path = package_dir / "workflow-request.json"
            event_path = request_root / "skill-events.jsonl"
            runtime_env[SKILL_EVENT_FILE_ENV] = str(event_path)
            request_payload: dict[str, object] = {
                "workflow_prompt": workflow_prompt,
                "skill_names": normalize_skill_names(skill_names),
            }
            if invocation_plan is not None:
                request_payload["invocation_plan"] = _request_invocation_plan(invocation_plan)
            failure_stage = "initialize"
            if pinned_packages is not None:
                entries = stage_packages(pinned_packages, package_dir)
                request_payload["pinned_packages"] = [entry.model_dump() for entry in entries]
                request_payload["pinned_protocol_version"] = 1
            request_path.write_text(
                json.dumps(request_payload, ensure_ascii=False), encoding="utf-8"
            )

            stdout_path = request_root / "stdout.log"
            stderr_path = request_root / "stderr.log"
            effective_timeout = timeout or self.timeout
            command = _agent_command(self.agent_path, request_path, pinned_packages is not None)
            failure_stage = "create"
            try:
                with (
                    stdout_path.open("wb") as stdout_stream,
                    stderr_path.open("wb") as stderr_stream,
                ):
                    process = subprocess.Popen(
                        command,
                        cwd=str(work_dir),
                        env=runtime_env,
                        stdout=stdout_stream,
                        stderr=stderr_stream,
                        start_new_session=starts_new_process_group(),
                    )
                    sandbox.update(creation_status="completed", instance_status="created")
                    skill_events.append(
                        SkillEvent.create(
                            "sandbox.session.created",
                            status="completed",
                            runtime_id=runtime_id,
                            attributes=lifecycle_attributes,
                        )
                    )
                    failure_stage = "execute"
                    timed_out = False
                    try:
                        process.wait(timeout=effective_timeout)
                    except subprocess.TimeoutExpired:
                        timed_out = True
                    finally:
                        process_cleanup_error = terminate_process_group(process)
                    exit_code = process.returncode
                    process = None
            except (OSError, ValueError) as exc:
                sandbox.update(creation_status="failed", instance_status="unknown")
                result = SkillRuntimeResult(
                    runtime_id=runtime_id,
                    exit_code=None,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
            else:
                stdout = _read_output(stdout_path)
                stderr = _read_output(stderr_path)
                if timed_out:
                    if process_cleanup_error:
                        sandbox.update(
                            artifact_collection_status="failed",
                            artifact_collection_error="process_cleanup_failed",
                        )
                        recovered_stdout, recovered_files = stdout, []
                    else:
                        recovered_stdout, recovered_files = self._recover_artifacts(
                            stdout, work_dir, request_root, sandbox
                        )
                    parsed = parse_workflow_result(recovered_stdout)
                    _record_control_observation(recovered_stdout, sandbox)
                    result = SkillRuntimeResult(
                        runtime_id=runtime_id,
                        exit_code=None,
                        stdout=recovered_stdout,
                        stderr=stderr,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        timed_out=True,
                        output_files=recovered_files,
                        output_text=parsed.output_text,
                        output_text_truncated=parsed.output_text_truncated,
                        workflow_status=parsed.workflow_status,
                        executed_skill=parsed.executed_skill,
                        instructions=parsed.instructions,
                        error_type="TimeoutExpired",
                        error_message=f"Skill workflow timed out after {effective_timeout}s",
                    )
                elif exit_code == 0 and process_cleanup_error:
                    failure_stage = "cleanup"
                    result = SkillRuntimeResult(
                        runtime_id=runtime_id,
                        exit_code=exit_code,
                        stdout=stdout,
                        stderr=stderr,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        error_type="ProcessCleanupError",
                        error_message="Local Skill runtime process group could not be stopped",
                    )
                else:
                    strict_collection = exit_code == 0
                    failure_stage = "collect" if strict_collection else "execute"
                    if process_cleanup_error:
                        sandbox.update(
                            artifact_collection_status="failed",
                            artifact_collection_error="process_cleanup_failed",
                        )
                        delivered_stdout, output_files = stdout, []
                    else:
                        delivered_stdout, output_files = self._deliver_artifacts(
                            stdout,
                            work_dir,
                            request_root,
                            sandbox,
                            strict=strict_collection,
                        )
                    parsed = parse_workflow_result(delivered_stdout)
                    _record_control_observation(delivered_stdout, sandbox)
                    result = SkillRuntimeResult(
                        runtime_id=runtime_id,
                        exit_code=exit_code,
                        stdout=delivered_stdout,
                        stderr=stderr,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        output_files=output_files,
                        output_text=parsed.output_text,
                        output_text_truncated=parsed.output_text_truncated,
                        workflow_status=parsed.workflow_status,
                        executed_skill=parsed.executed_skill,
                        instructions=parsed.instructions,
                        error_type=parsed.command_error_type or None,
                        error_message=parsed.error or None,
                    )
                    if exit_code == 0:
                        failure_stage = ""
        except BaseException as exc:
            primary_error = exc
            primary_traceback = exc.__traceback__
        finally:
            if process is not None:
                process_cleanup_error = terminate_process_group(process) or process_cleanup_error
            if event_path is not None:
                try:
                    skill_events.extend(
                        replace(event, runtime_id=event.runtime_id or runtime_id)
                        for event in _read_request_events(
                            event_path,
                            expected_invocations=_expected_invocations(invocation_plan),
                        )
                    )
                except Exception:
                    pass

            cleanup_error = process_cleanup_error
            if request_root is not None:
                try:
                    _remove_request_directory(request_root)
                except Exception:
                    cleanup_error = cleanup_error or "request_directory_cleanup_failed"
            if cleanup_error:
                sandbox.update(cleanup_status="failed", cleanup_error="cleanup_failed")
                if not failure_stage:
                    failure_stage = "cleanup"
                skill_events.append(
                    SkillEvent.create(
                        "sandbox.session.cleanup_failed",
                        status="failed",
                        runtime_id=runtime_id,
                        error_category="cleanup_failed",
                        attributes=lifecycle_attributes,
                    )
                )
            elif request_root is not None:
                sandbox.update(cleanup_status="completed", cleanup_error=None)
                skill_events.append(
                    SkillEvent.create(
                        "sandbox.session.cleaned_up",
                        status="completed",
                        runtime_id=runtime_id,
                        attributes=lifecycle_attributes,
                    )
                )
            else:
                sandbox.update(cleanup_status="not_started", cleanup_error=None)
            if failure_stage:
                sandbox["failure_stage"] = failure_stage

        if primary_error is not None:
            _attach_exception_diagnostics(primary_error, sandbox, skill_events)
            raise primary_error.with_traceback(primary_traceback)
        assert result is not None
        return replace(result, skill_events=skill_events, sandbox=sandbox)

    def _deliver_artifacts(
        self,
        stdout: str,
        work_dir: Path,
        request_root: Path,
        sandbox: dict[str, object],
        *,
        strict: bool,
    ) -> tuple[str, list[str]]:
        try:
            payload = _workflow_payload(stdout)
            paths = payload.get("output_files")
            if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
                raise SkillRuntimeError("Runtime returned invalid artifact paths")
            bundle_path = request_root / "artifacts.zip"
            receipt = export_artifacts(paths, work_dir, bundle_path)
            output_files = import_artifacts(
                bundle_path.read_bytes(), receipt, parent=self.artifact_directory
            )
            payload["output_files"] = output_files
            payload["artifacts"] = output_files
            payload["artifact_bundle"] = receipt.model_dump()
            sandbox["artifact_collection_status"] = "completed"
            return _rewrite_workflow_payload(stdout, payload), output_files
        except Exception as exc:
            sandbox.update(
                artifact_collection_status="failed",
                artifact_collection_error=type(exc).__name__,
            )
            if strict:
                raise
            return stdout, []

    def _recover_artifacts(
        self,
        stdout: str,
        work_dir: Path,
        request_root: Path,
        sandbox: dict[str, object],
    ) -> tuple[str, list[str]]:
        delivered_stdout, delivered_files = self._deliver_artifacts(
            stdout, work_dir, request_root, sandbox, strict=False
        )
        if sandbox.get("artifact_collection_status") == "completed":
            sandbox["artifact_collection_status"] = "recovered_partial"
            return delivered_stdout, delivered_files
        try:
            known_paths = _known_output_artifacts(work_dir / "artifacts")
            if not known_paths:
                return stdout, []
            bundle_path = request_root / "recovered-artifacts.zip"
            receipt = export_artifacts(known_paths, work_dir, bundle_path)
            output_files = import_artifacts(
                bundle_path.read_bytes(), receipt, parent=self.artifact_directory
            )
            sandbox.update(
                artifact_collection_status="recovered_partial",
                artifact_collection_source="request_output_directory",
            )
            sandbox.pop("artifact_collection_error", None)
            return stdout, output_files
        except Exception as exc:
            sandbox.update(
                artifact_collection_status="failed",
                artifact_collection_error=type(exc).__name__,
            )
            return stdout, []


def _runtime_environment(
    env: dict[str, str] | None,
    *,
    pinned: bool,
    extra_env_names: tuple[str, ...] = (),
) -> dict[str, str]:
    base_allowed = {
        "PATH",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "SYSTEMROOT",
        "KSADK_SKILL_RUNTIME_TIMEOUT",
        "KSADK_SKILL_OUTPUT_TEXT_MAX_BYTES",
        "KSADK_SKILL_ARTIFACT_PROJECT",
    }
    service_allowed = {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "KSADK_SKILL_SERVICE_URL",
        "KSADK_SKILL_SERVICE_ENDPOINT",
        "KSADK_SKILL_SERVICE_SCHEME",
        "KSADK_SKILL_SERVICE_ACCOUNT_ID",
        "KSADK_SKILL_SERVICE_REGION",
        "KSADK_SKILL_SERVICE_TOKEN",
        "KSADK_SKILL_MANIFEST_TIMEOUT",
        "KSADK_SKILL_CACHE_DIR",
        "KSADK_LOCAL_SKILLS_DIR",
        "KSADK_SKILL_ALLOW_HASH_MISMATCH",
        "KSADK_PUBLIC_SKILL_ALLOWLIST",
        "AGENTENGINE_PRE_CONTROL_REGION",
        "KSYUN_REGION",
    }
    allowed = base_allowed | (set() if pinned else service_allowed)
    allowed.update(extra_env_names)
    runtime_env = {key: value for key, value in os.environ.items() if key in allowed}
    # Explicit call configuration belongs to the trusted runtime. It does not
    # become Skill script environment unless separately allowlisted.
    runtime_env.update(
        {
            key: value
            for key, value in (env or {}).items()
            if not key.startswith("KSADK_LOCAL_PROCESS_")
        }
    )
    return sandbox_runtime_env(runtime_env)


def _request_parent(env: dict[str, str] | None) -> str | None:
    configured = (env or {}).get("KSADK_SKILL_WORKDIR") or os.environ.get("KSADK_SKILL_WORKDIR")
    if not configured:
        return None
    parent = Path(configured).expanduser().resolve()
    parent.mkdir(parents=True, exist_ok=True)
    return str(parent)


def _bundled_agent_path() -> Path:
    return (Path(__file__).resolve().parents[1] / "agent.py").resolve()


def _trusted_sdk_root() -> Path:
    # local.py is <trusted-root>/ksadk/skills/runtime/backends/local.py. The
    # imported backend module, never cwd/PYTHONPATH, is the authority for this root.
    return Path(__file__).resolve().parents[4]


def _agent_command(agent_path: Path, request_path: Path, pinned: bool) -> list[str]:
    if not pinned:
        return [
            sys.executable,
            "-u",
            str(agent_path),
            "--request-file",
            str(request_path),
        ]
    bootstrap = (
        "import sys;"
        "sys.path.insert(0,sys.argv.pop(1));"
        "from ksadk.skills.runtime.agent import main;"
        "raise SystemExit(main(sys.argv[1:]))"
    )
    return [
        sys.executable,
        "-I",
        "-u",
        "-c",
        bootstrap,
        str(_trusted_sdk_root()),
        "--request-file",
        str(request_path),
    ]


def _workflow_payload(stdout: str) -> dict[str, object]:
    raw_payloads = [
        line.split("=", 1)[1] for line in stdout.splitlines() if line.startswith("workflow_result=")
    ]
    if len(raw_payloads) != 1:
        raise SkillRuntimeError("Runtime did not return a unique workflow result")
    try:
        payload = json.loads(raw_payloads[0])
    except json.JSONDecodeError:
        raise SkillRuntimeError("Runtime returned a malformed workflow result") from None
    if not isinstance(payload, dict):
        raise SkillRuntimeError("Runtime did not return a unique workflow result")
    return payload


def _record_control_observation(stdout: str, sandbox: dict[str, object]) -> None:
    try:
        payload = _workflow_payload(stdout)
    except SkillRuntimeError:
        return
    commands = payload.get("commands")
    if not isinstance(commands, list):
        return
    observations: list[dict[str, object]] = []
    for command in commands:
        if not isinstance(command, dict):
            continue
        controls = command.get("controls")
        observation: dict[str, object] = {}
        if isinstance(controls, dict):
            status = controls.get("status")
            if status in {"applied", "partial", "failed", "blocked"}:
                observation["status"] = status
            applied = controls.get("applied")
            if isinstance(applied, dict):
                observation["applied_limits"] = sorted(
                    name
                    for name, value in applied.items()
                    if isinstance(name, str) and type(value) is int
                )
            unsupported = controls.get("unsupported")
            if isinstance(unsupported, list):
                observation["unsupported_limits"] = [
                    item for item in unsupported if isinstance(item, str)
                ][:10]
            static_check = controls.get("static_check")
            if isinstance(static_check, str):
                observation["static_check"] = static_check
        for key in (
            "error_type",
            "blocked_reason",
            "resource_limit",
            "timed_out",
            "output_limit_exceeded",
            "stdout_truncated",
            "stderr_truncated",
            "cleanup_status",
        ):
            value = command.get(key)
            if isinstance(value, (str, bool)):
                observation[key] = value
        if observation:
            observations.append(observation)
    if observations:
        sandbox["controls"] = observations


def _rewrite_workflow_payload(stdout: str, payload: dict[str, object]) -> str:
    return (
        "\n".join(
            "workflow_result=" + json.dumps(payload, ensure_ascii=False, sort_keys=True)
            if line.startswith("workflow_result=")
            else line
            for line in stdout.splitlines()
        )
        + "\n"
    )


def _read_output(path: Path) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as source:
        size = path.stat().st_size
        if size > _MAX_PROCESS_OUTPUT_BYTES:
            source.seek(size - _MAX_PROCESS_OUTPUT_BYTES)
        content = source.read(_MAX_PROCESS_OUTPUT_BYTES)
    return content.decode("utf-8", errors="replace")


def _known_output_artifacts(output_dir: Path) -> list[str]:
    """Find only a bounded set in the runtime's declared output directory."""

    if not output_dir.is_dir():
        return []
    paths: list[str] = []
    visited = 0
    for root, directories, files in os.walk(output_dir, followlinks=False):
        directories[:] = [name for name in directories if not (Path(root) / name).is_symlink()]
        visited += len(directories) + len(files)
        if visited > MAX_FILES * 4:
            raise ArtifactDeliveryError("Artifact recovery scan limit exceeded")
        for name in files:
            paths.append(str(Path(root) / name))
            if len(paths) > MAX_FILES:
                raise ArtifactDeliveryError("Too many artifacts")
    return sorted(paths)


def _read_request_events(
    path: Path, *, expected_invocations: dict[str, object] | None
) -> list[SkillEvent]:
    try:
        with path.open("rb") as source:
            content = source.read(_MAX_EVENT_FILE_BYTES + 1)
        if len(content) > _MAX_EVENT_FILE_BYTES:
            return []
        return parse_sandbox_skill_event_lines(
            content.decode("utf-8", errors="replace"),
            expected_invocations=expected_invocations,
        )
    except OSError:
        return []


def _remove_request_directory(path: Path) -> None:
    shutil.rmtree(path)


def _attach_exception_diagnostics(
    exc: BaseException, sandbox: dict[str, object], skill_events: list[SkillEvent]
) -> None:
    try:
        setattr(exc, "sandbox", dict(sandbox))
        setattr(exc, "skill_events", list(skill_events))
    except Exception:
        pass
    if sandbox.get("cleanup_status") == "failed":
        try:
            exc.add_note("Local Skill runtime request cleanup failed; see .sandbox diagnostics")
        except (AttributeError, TypeError):
            pass


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
