"""Local source evaluation target backed by the unified RuntimeAdapter stack."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ksadk.detection.detector import DetectionResult, FrameworkDetector, FrameworkType
from ksadk.evaluation.adapters import TargetAdapterError
from ksadk.evaluation.contracts import (
    EvalCase,
    EvalRunSpec,
    TargetKind,
    TargetRef,
    TargetRun,
    TargetRunStatus,
    TargetSnapshot,
    ToolCallEvidence,
    TraceRef,
    UsageSnapshot,
)
from ksadk.evaluation.evidence import EvidenceStore, project_tool_calls
from ksadk.events.store import RuntimeEventStore
from ksadk.runtime import RuntimeExecutor, RuntimeLaunchContext
from ksadk.runtime.conversation_execution import invoke_runtime_conversation_once
from ksadk.runtime.factory import build_default_runtime_registry
from ksadk.sessions.in_memory import InMemorySessionService

_SUPPORTED_FRAMEWORKS = {
    FrameworkType.ADK: "adk",
    FrameworkType.LANGGRAPH: "langgraph",
    FrameworkType.LANGCHAIN: "langgraph",
    FrameworkType.DEEPAGENTS: "langgraph",
}
_EXCLUDED_DIRECTORIES = {
    ".agentengine",
    ".agentkit",
    ".aws",
    ".azure",
    ".docker",
    ".git",
    ".hg",
    ".kube",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".ssh",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "tmp",
    "venv",
}
_EXCLUDED_FILE_NAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "auth.json",
    "credentials.json",
    "dockerconfigjson",
    "kubeconfig",
    "secrets.json",
    "service-account.json",
}
_EXCLUDED_SUFFIXES = {
    ".db",
    ".jks",
    ".key",
    ".keystore",
    ".log",
    ".p12",
    ".pem",
    ".pfx",
    ".pyc",
    ".pyo",
    ".sqlite",
}
_MAX_SNAPSHOT_FILES = 10_000
_MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024

_Invoke = Callable[..., Awaitable[tuple[str, dict[str, Any]]]]


@dataclass(frozen=True)
class _ResolvedLocalTarget:
    snapshot: TargetSnapshot
    detection: DetectionResult
    launch_context: RuntimeLaunchContext
    agent_id: str
    workspace: tempfile.TemporaryDirectory


@dataclass(frozen=True)
class _LocalTurnResult:
    output: str
    usage: UsageSnapshot
    invocation_id: str


@dataclass(frozen=True)
class _LocalCaseResult:
    status: TargetRunStatus
    turns: tuple[_LocalTurnResult, ...]
    duration_ms: int
    error_code: str | None = None
    error_message: str | None = None
    trace_ref: TraceRef | None = None
    trace_refs: tuple[TraceRef, ...] = ()
    tool_calls: tuple[ToolCallEvidence, ...] = ()


class LocalTargetError(TargetAdapterError):
    """Classified failure while resolving a local source target."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


class LocalSourceTargetAdapter:
    """Snapshot local ADK/LangGraph-family projects for evaluation."""

    kind = TargetKind.LOCAL_SOURCE

    def __init__(
        self,
        *,
        timeout_seconds: int,
        invoke: _Invoke = invoke_runtime_conversation_once,
        evidence_store: EvidenceStore | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._invoke = invoke
        self._evidence_store = evidence_store
        self._executor = RuntimeExecutor(build_default_runtime_registry())
        self._session_service = InMemorySessionService()
        self._resolved: _ResolvedLocalTarget | None = None

    async def snapshot(self, target: TargetRef) -> TargetSnapshot:
        if target.kind is not self.kind:
            raise LocalTargetError(
                "LOCAL_TARGET_KIND_INVALID",
                "Local Source adapter received a non-local target",
            )

        project_dir = await asyncio.to_thread(_resolve_project_dir, target.locator)
        workspace, source_digest = await asyncio.to_thread(_materialize_snapshot, project_dir)
        snapshot_dir = Path(workspace.name)
        try:
            detection = await asyncio.to_thread(
                lambda: FrameworkDetector(str(snapshot_dir)).detect()
            )
        except BaseException:
            workspace.cleanup()
            raise
        runtime_type = _SUPPORTED_FRAMEWORKS.get(detection.type)
        if runtime_type is None:
            workspace.cleanup()
            raise LocalTargetError(
                "LOCAL_FRAMEWORK_UNSUPPORTED",
                f"Unsupported local Agent framework: {detection.type.value}",
            )

        try:
            entrypoint = await asyncio.to_thread(
                _resolve_entrypoint,
                snapshot_dir,
                target.entrypoint or detection.entry_point,
            )
        except BaseException:
            workspace.cleanup()
            raise
        detection = replace(detection, entry_point=entrypoint.as_posix())
        git_head, git_dirty = await asyncio.to_thread(_git_state, project_dir)
        metadata: dict[str, object] = {
            "detectedFramework": detection.type.value,
            "agentVariable": detection.agent_variable,
        }
        if git_head is not None:
            metadata["gitHead"] = git_head
        if git_dirty is not None:
            metadata["gitDirty"] = git_dirty

        snapshot = TargetSnapshot(
            kind=self.kind,
            entrypoint=entrypoint.as_posix(),
            revision_digest=f"sha256:{source_digest}",
            runtime=runtime_type,
            metadata=metadata,
        )
        previous = self._resolved
        self._resolved = _ResolvedLocalTarget(
            snapshot=snapshot,
            detection=detection,
            launch_context=RuntimeLaunchContext(
                runtime_type=runtime_type,
                project_dir=snapshot_dir,
                detection=detection,
                config={
                    **dict(detection.raw_config or {}),
                    "turn_timeout_seconds": self._timeout_seconds,
                },
            ),
            agent_id=detection.name or project_dir.name,
            workspace=workspace,
        )
        if previous is not None:
            previous.workspace.cleanup()
        return snapshot

    async def run_case(
        self,
        spec: EvalRunSpec,
        case: EvalCase,
        *,
        attempt: int,
    ) -> TargetRun:
        resolved = self._resolved
        if resolved is None:
            raise RuntimeError("Local Source target must be snapshotted before execution")
        if spec.target != resolved.snapshot:
            raise RuntimeError("EvalRunSpec target does not match the snapshotted local target")

        started_at = time.perf_counter()
        turns: list[_LocalTurnResult] = []
        session_id = _scoped_id("eval-session", spec.id, case.id, str(attempt))
        invocation_id: str | None = None
        trace_ref: TraceRef | None = None
        trace_refs: list[TraceRef] = []
        tool_calls: list[ToolCallEvidence] = []
        try:
            for turn_index, turn in enumerate(case.turns, start=1):
                invocation_id = _scoped_id(
                    "eval-invocation",
                    spec.id,
                    case.id,
                    str(attempt),
                    str(turn_index),
                )
                session_id, runtime_result = await asyncio.wait_for(
                    self._invoke(
                        executor=self._executor,
                        launch_context=resolved.launch_context,
                        agent_id=resolved.agent_id,
                        user_id="eval-user",
                        messages=[{"role": "user", "content": turn.input}],
                        session_id=session_id,
                        model=_configured_model(resolved.detection),
                        invocation_id=invocation_id,
                        session_service_provider=lambda: self._session_service,
                    ),
                    timeout=self._timeout_seconds,
                )
                turns.append(
                    _LocalTurnResult(
                        output=str(runtime_result.get("output_text") or ""),
                        usage=_usage_snapshot(runtime_result.get("usage")),
                        invocation_id=invocation_id,
                    )
                )
                if self._evidence_store is not None:
                    events = await RuntimeEventStore(self._session_service).list(
                        session_id,
                        run_id=invocation_id,
                    )
                    if events:
                        trace_ref = self._evidence_store.write_trace(
                            spec.id,
                            events,
                            session_id=session_id,
                            policy=spec.config.data_policy,
                        )
                        trace_refs.append(trace_ref)
                        tool_calls.extend(project_tool_calls(events))
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            result = _LocalCaseResult(
                status=TargetRunStatus.ERROR,
                turns=tuple(turns),
                duration_ms=_elapsed_ms(started_at),
                error_code="LOCAL_RUNTIME_TIMEOUT",
                error_message="Local Agent runtime timed out",
                trace_ref=trace_ref,
                trace_refs=tuple(trace_refs),
                tool_calls=tuple(tool_calls),
            )
            return _to_target_run(result, runtime=resolved.snapshot.runtime)
        except Exception:
            result = _LocalCaseResult(
                status=TargetRunStatus.ERROR,
                turns=tuple(turns),
                duration_ms=_elapsed_ms(started_at),
                error_code="LOCAL_RUNTIME_ERROR",
                error_message="Local Agent runtime failed",
                trace_ref=trace_ref,
                trace_refs=tuple(trace_refs),
                tool_calls=tuple(tool_calls),
            )
            return _to_target_run(result, runtime=resolved.snapshot.runtime)
        finally:
            await asyncio.shield(self._session_service.delete_session(session_id))

        status = (
            TargetRunStatus.PASSED if turns and turns[-1].output else TargetRunStatus.UNAVAILABLE
        )
        result = _LocalCaseResult(
            status=status,
            turns=tuple(turns),
            duration_ms=_elapsed_ms(started_at),
            error_code=(None if status is TargetRunStatus.PASSED else "LOCAL_OUTPUT_UNAVAILABLE"),
            error_message=(
                None
                if status is TargetRunStatus.PASSED
                else "Local Agent did not provide evaluable text output"
            ),
            trace_ref=trace_ref,
            trace_refs=tuple(trace_refs),
            tool_calls=tuple(tool_calls),
        )
        return _to_target_run(result, runtime=resolved.snapshot.runtime)


def _resolve_project_dir(locator: str) -> Path:
    project_dir = Path(locator).expanduser().resolve()
    if not project_dir.is_dir():
        raise LocalTargetError(
            "LOCAL_PROJECT_INVALID",
            "Local Source target locator must be an existing directory",
        )
    return project_dir


def _resolve_entrypoint(project_dir: Path, value: str) -> Path:
    if not value:
        raise LocalTargetError(
            "LOCAL_ENTRYPOINT_INVALID",
            "Local Agent entrypoint was not detected",
        )
    project_dir = project_dir.resolve()
    candidate = (project_dir / Path(value.replace("\\", "/"))).resolve()
    try:
        relative = candidate.relative_to(project_dir)
    except ValueError as exc:
        raise LocalTargetError(
            "LOCAL_ENTRYPOINT_INVALID",
            "Local Agent entrypoint must remain inside the project directory",
        ) from exc
    if not candidate.is_file():
        raise LocalTargetError(
            "LOCAL_ENTRYPOINT_INVALID",
            "Local Agent entrypoint must be an existing file",
        )
    return relative


def _materialize_snapshot(
    project_dir: Path,
) -> tuple[tempfile.TemporaryDirectory, str]:
    workspace = tempfile.TemporaryDirectory(prefix="ksadk-eval-local-")
    snapshot_dir = Path(workspace.name)
    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    try:
        for path in _snapshot_files(project_dir):
            relative = path.relative_to(project_dir)
            if (
                not path.is_file()
                or _exclude_from_snapshot(relative)
                or not _is_within_project(path, project_dir)
            ):
                continue
            file_count += 1
            if file_count > _MAX_SNAPSHOT_FILES:
                raise LocalTargetError(
                    "LOCAL_SNAPSHOT_TOO_LARGE",
                    "Local Agent snapshot exceeds the supported size limit",
                )
            destination = snapshot_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest.update(relative.as_posix().encode("utf-8"))
            digest.update(b"\0")
            with path.open("rb") as source, destination.open("wb") as target:
                while chunk := source.read(_HASH_CHUNK_BYTES):
                    total_bytes += len(chunk)
                    if total_bytes > _MAX_SNAPSHOT_BYTES:
                        raise LocalTargetError(
                            "LOCAL_SNAPSHOT_TOO_LARGE",
                            "Local Agent snapshot exceeds the supported size limit",
                        )
                    digest.update(chunk)
                    target.write(chunk)
            digest.update(b"\0")
        return workspace, digest.hexdigest()
    except LocalTargetError:
        workspace.cleanup()
        raise
    except OSError as exc:
        workspace.cleanup()
        raise LocalTargetError(
            "LOCAL_SNAPSHOT_FAILED",
            "Unable to materialize a Local Agent snapshot input",
        ) from exc


def _snapshot_files(project_dir: Path) -> list[Path]:
    files: list[Path] = []
    for root, directories, names in os.walk(project_dir, followlinks=False):
        directories[:] = sorted(
            name for name in directories if name.lower() not in _EXCLUDED_DIRECTORIES
        )
        root_path = Path(root)
        files.extend(root_path / name for name in sorted(names))
    return files


def _configured_model(detection: DetectionResult) -> str | None:
    value = detection.raw_config.get("model") if detection.raw_config else None
    model = str(value or "").strip()
    return model or None


def _usage_snapshot(raw_usage: object) -> UsageSnapshot:
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    reported = any(key in usage for key in ("input_tokens", "output_tokens", "total_tokens"))
    return UsageSnapshot(
        input_tokens=_non_negative_int(usage.get("input_tokens")),
        output_tokens=_non_negative_int(usage.get("output_tokens")),
        total_tokens=_non_negative_int(usage.get("total_tokens")),
        reported=reported,
    )


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _sum_usage(turns: tuple[_LocalTurnResult, ...]) -> UsageSnapshot:
    reported = any(turn.usage.reported for turn in turns)
    return UsageSnapshot(
        input_tokens=sum(turn.usage.input_tokens for turn in turns),
        output_tokens=sum(turn.usage.output_tokens for turn in turns),
        total_tokens=sum(turn.usage.total_tokens for turn in turns),
        reported=reported,
    )


def _to_target_run(result: _LocalCaseResult, *, runtime: str) -> TargetRun:
    final_turn = result.turns[-1] if result.turns else None
    return TargetRun(
        status=result.status,
        output=(
            final_turn.output
            if final_turn is not None and result.status is TargetRunStatus.PASSED
            else ""
        ),
        duration_ms=result.duration_ms,
        usage=_sum_usage(result.turns),
        error_code=result.error_code,
        error_message=result.error_message,
        trace_ref=result.trace_ref,
        trace_refs=list(result.trace_refs),
        tool_calls=list(result.tool_calls),
        metadata={
            "runtime": runtime,
            "turnCount": len(result.turns),
        },
    )


def _scoped_id(prefix: str, *parts: str) -> str:
    payload = "\0".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:24]}"


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.perf_counter() - started_at) * 1000))


def _exclude_from_snapshot(relative: Path) -> bool:
    if any(part.lower() in _EXCLUDED_DIRECTORIES for part in relative.parts[:-1]):
        return True
    name = relative.name.lower()
    if name in _EXCLUDED_FILE_NAMES:
        return True
    if name.startswith(".env.") and name not in {
        ".env.example",
        ".env.sample",
        ".env.template",
    }:
        return True
    return relative.suffix.lower() in _EXCLUDED_SUFFIXES


def _is_within_project(path: Path, project_dir: Path) -> bool:
    try:
        path.resolve().relative_to(project_dir)
        return True
    except (OSError, ValueError):
        return False


def _git_state(project_dir: Path) -> tuple[str | None, bool | None]:
    try:
        head = _run_git(project_dir, "rev-parse", "HEAD")
        dirty = bool(
            _run_git(
                project_dir,
                "status",
                "--porcelain",
                "--untracked-files=normal",
                "--",
                ".",
            )
        )
        return head, dirty
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, None


def _run_git(project_dir: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(project_dir), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return completed.stdout.strip()


__all__ = ["LocalSourceTargetAdapter", "LocalTargetError"]
