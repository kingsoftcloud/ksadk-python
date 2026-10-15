"""Host-owned deterministic file verification with durable scoped evidence.

Assertions and this verifier's code must remain outside the agent's writable
environment. Merely using a separate directory does not sandbox a process with
arbitrary filesystem access: the host must restrict its tools or use separate
permissions. The host must also freeze the workspace (or recheck its digest)
between the final read here and committing a verified outcome.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .completion import (
    AcceptanceItem,
    CompletionCandidate,
    VerificationItemResult,
    VerificationRepairTarget,
    VerificationReport,
)
from .ports import VerificationBundle, VerificationRequest
from .state import RunScope

NonBlank = Annotated[str, Field(min_length=1, pattern=r"\S")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_REFERENCE = re.compile(
    r"native-file-evidence://([0-9a-f]{64})/([0-9a-f]{64})/([0-9a-f]{64})/([0-9a-f]{64})"
)
_MAX_EVIDENCE_BYTES = 1024 * 1024


class _Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FileAssertion(_Contract):
    relative_path: NonBlank
    expected_sha256: Sha256 | None = Field(default=None, repr=False)
    expected_text: str | None = Field(default=None, repr=False)

    @field_validator("relative_path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        if (
            "\\" in value
            or "\x00" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("file assertion requires a normalized relative path")
        return value

    @model_validator(mode="after")
    def has_concrete_assertion(self) -> FileAssertion:
        if self.expected_sha256 is None and self.expected_text is None:
            raise ValueError("at least one concrete content assertion is required")
        return self


class FileEvidenceItem(_Contract):
    acceptance_id: NonBlank
    relative_path: str | None
    status: Literal["passed", "failed", "not_run", "blocked"]
    actual_sha256: Sha256 | None
    size_bytes: int | None
    safe_summary: NonBlank


class FileEvidence(_Contract):
    schema_version: Literal["ksadk.native.file-evidence/v1"] = "ksadk.native.file-evidence/v1"
    scope_digest: Sha256
    run_digest: Sha256
    candidate_digest: Sha256
    contract_revision: NonBlank
    verifier_id: Literal["native.file"] = "native.file"
    verifier_version: Literal["1"] = "1"
    verification_method: Literal["deterministic"] = "deterministic"
    subject_digest: Sha256
    # Optional on read for v1 evidence written before execution-context binding.
    subject_context_digest: NonBlank | None = None
    environment_digest: NonBlank
    items: tuple[FileEvidenceItem, ...]


@dataclass(frozen=True)
class _FileRead:
    data: bytes | None = None
    sha256: str | None = None
    size: int | None = None
    reason: str = "file_missing"

    def manifest(self) -> dict[str, str | int | None]:
        return {"sha256": self.sha256, "size": self.size, "reason": self.reason}


def _encode(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _identity(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino


def _open_directory(path: Path, identity: tuple[int, int]) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if (info.st_dev, info.st_ino) != identity:
        os.close(fd)
        raise PermissionError("pinned root directory identity changed")
    return fd


def _repair_target(item: FileEvidenceItem) -> VerificationRepairTarget | None:
    if item.status != "failed" or item.relative_path is None:
        return None
    try:
        return VerificationRepairTarget(kind="workspace_file", path=item.relative_path)
    except ValueError:
        # Existing file assertions can name POSIX files (e.g. containing a
        # colon) that the portable repair-path contract cannot represent.
        # Keep their diagnosis without invalidating the verification report.
        return None


class FileVerificationPort:
    """Scope-pinned file delivery verifier; implements ``VerificationPort``.

    ``subject_digest`` covers all files targeted by this contract, not unrelated
    workspace files. Its manifest uses actual bytes, never Git HEAD or mtime.
    Symlinks, hardlinked files and non-regular files are intentionally rejected.
    """

    def __init__(
        self,
        *,
        scope: RunScope,
        workspace_root: Path | str,
        evidence_root: Path | str,
        environment_digest: str,
        assertions: Mapping[str, FileAssertion],
        max_file_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if type(max_file_bytes) is not int or max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be a positive integer")
        if not isinstance(environment_digest, str) or not environment_digest.strip():
            raise ValueError("environment_digest must be nonempty")
        if len(assertions) > 128:
            raise ValueError("file verifier supports at most 128 host assertions")
        if any(
            not isinstance(key, str) or not key.strip() or not isinstance(value, FileAssertion)
            for key, value in assertions.items()
        ):
            raise ValueError("assertions must map nonempty acceptance IDs to FileAssertion")
        workspace = Path(workspace_root).resolve(strict=True)
        evidence = Path(evidence_root).resolve()
        if evidence.is_relative_to(workspace) or workspace.is_relative_to(evidence):
            raise ValueError("evidence root must be outside and disjoint from workspace")
        if not workspace.is_dir():
            raise ValueError("workspace root must be a directory")
        evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._scope = scope
        self._scope_digest = _hash(_encode(scope.model_dump(mode="json")))
        self._workspace = workspace
        self._evidence_root = evidence
        self._workspace_identity = _identity(workspace)
        self._evidence_identity = _identity(evidence)
        self._environment_digest = environment_digest
        self._assertions = MappingProxyType(dict(assertions))
        self._max_file_bytes = max_file_bytes

    async def verify(self, request: VerificationRequest) -> VerificationBundle:
        if request.scope != self._scope:
            raise PermissionError("verification scope mismatch")
        return await asyncio.to_thread(self._verify, request)

    def _read_file(self, relative_path: str) -> _FileRead:
        directory_fd = None
        try:
            directory_fd = _open_directory(self._workspace, self._workspace_identity)
            parts = relative_path.split("/")
            for part in parts[:-1]:
                next_fd = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
                )
                os.close(directory_fd)
                directory_fd = next_fd
            fd = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
            )
            with os.fdopen(fd, "rb") as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    return _FileRead(reason="target_not_regular_unlinked_file")
                if before.st_size > self._max_file_bytes:
                    return _FileRead(size=before.st_size, reason="file_size_limit_exceeded")
                data = handle.read(self._max_file_bytes + 1)
                after = os.fstat(handle.fileno())
            if len(data) > self._max_file_bytes:
                return _FileRead(size=len(data), reason="file_size_limit_exceeded")
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                return _FileRead(reason="target_changed_during_read")
            return _FileRead(data=data, sha256=_hash(data), size=len(data), reason="file_read")
        except FileNotFoundError:
            return _FileRead()
        except (OSError, ValueError):
            return _FileRead(reason="unsafe_or_unreadable_target")
        finally:
            if directory_fd is not None:
                os.close(directory_fd)

    def _read_targets(self, paths: tuple[str, ...]) -> dict[str, _FileRead]:
        return {path: self._read_file(path) for path in paths}

    def _item(self, acceptance: AcceptanceItem, reads: dict[str, _FileRead]) -> FileEvidenceItem:
        assertion = self._assertions.get(acceptance.acceptance_id)
        relative_path = assertion.relative_path if assertion else None
        observation = reads.get(relative_path) if relative_path is not None else None
        status = "not_run"
        reason = "host_assertion_missing"
        if (
            acceptance.verifier_ref,
            acceptance.verifier_version,
            acceptance.verification_method,
        ) != ("native.file", "1", "deterministic"):
            reason = "unsupported_verifier_pin"
        elif assertion is not None and observation is not None:
            status, reason = self._evaluate(assertion, observation)
        return FileEvidenceItem(
            acceptance_id=acceptance.acceptance_id,
            relative_path=relative_path,
            status=status,
            actual_sha256=observation.sha256 if observation else None,
            size_bytes=observation.size if observation else None,
            safe_summary=reason,
        )

    @staticmethod
    def _evaluate(assertion: FileAssertion, observation: _FileRead) -> tuple[str, str]:
        if observation.data is None:
            return (
                "failed" if observation.reason == "file_missing" else "blocked",
                observation.reason,
            )
        text_matches = (
            assertion.expected_text is None
            or observation.data == assertion.expected_text.encode("utf-8")
        )
        hash_matches = (
            assertion.expected_sha256 is None or observation.sha256 == assertion.expected_sha256
        )
        return (
            ("passed", "content_assertion_passed")
            if text_matches and hash_matches
            else ("failed", "content_assertion_failed")
        )

    def _verify(self, request: VerificationRequest) -> VerificationBundle:
        paths = tuple(
            sorted(
                {
                    self._assertions[item.acceptance_id].relative_path
                    for item in request.contract.items
                    if item.acceptance_id in self._assertions
                }
            )
        )
        before = self._read_targets(paths)
        items = tuple(self._item(item, before) for item in request.contract.items)
        after = self._read_targets(paths)
        subject = _hash(_encode({path: value.manifest() for path, value in after.items()}))
        if before != after:
            items = tuple(
                item.model_copy(
                    update={
                        "status": "blocked",
                        "safe_summary": "target_changed_during_verification",
                        "actual_sha256": after[item.relative_path].sha256
                        if item.relative_path is not None
                        else None,
                        "size_bytes": after[item.relative_path].size
                        if item.relative_path is not None
                        else None,
                    }
                )
                for item in items
            )
        evidence = FileEvidence(
            scope_digest=self._scope_digest,
            run_digest=_hash(request.run_id.encode()),
            candidate_digest=_hash(request.candidate_id.encode()),
            contract_revision=request.contract.revision,
            subject_digest=subject,
            subject_context_digest=request.subject_context_digest,
            environment_digest=self._environment_digest,
            items=items,
        )
        ref = self._persist_evidence(evidence)
        return self._bundle(request, subject, items, ref)

    def _bundle(
        self,
        request: VerificationRequest,
        subject: str,
        items: tuple[FileEvidenceItem, ...],
        ref: str,
    ) -> VerificationBundle:
        required_ids = {item.acceptance_id for item in request.contract.items if item.required}
        required = tuple(item for item in items if item.acceptance_id in required_ids)
        failures = [item.model_dump(mode="json") for item in required if item.status == "failed"]
        report = VerificationReport(
            candidate_id=request.candidate_id,
            contract_revision=request.contract.revision,
            subject_digest=subject,
            subject_context_digest=request.subject_context_digest,
            environment_digest=self._environment_digest,
            items=tuple(
                VerificationItemResult(
                    acceptance_id=item.acceptance_id,
                    status=item.status,
                    verifier_id="native.file",
                    verifier_version="1",
                    verification_method="deterministic",
                    evidence_refs=(ref,),
                    safe_summary=item.safe_summary,
                    repair_target=_repair_target(item),
                )
                for item in items
            ),
            repairable=bool(failures)
            and not any(item.status in {"blocked", "not_run"} for item in required),
            failure_fingerprint=_hash(_encode(failures)) if failures else "",
        )
        return VerificationBundle(
            candidate=CompletionCandidate(
                candidate_id=request.candidate_id,
                final_text=request.final_text,
                subject_digest=subject,
                subject_context_digest=request.subject_context_digest,
                environment_digest=self._environment_digest,
            ),
            report=report,
        )

    def _persist_evidence(self, evidence: FileEvidence) -> str:
        payload = _encode(evidence.model_dump(mode="json"))
        if len(payload) > _MAX_EVIDENCE_BYTES:
            raise ValueError("evidence size limit exceeded")
        parts = (
            evidence.scope_digest,
            evidence.run_digest,
            evidence.candidate_digest,
            _hash(payload),
        )
        ref = "native-file-evidence://" + "/".join(parts)
        filename = _hash(ref.encode()) + ".json"
        temporary = ".pending-" + uuid.uuid4().hex
        directory_fd = _open_directory(self._evidence_root, self._evidence_identity)
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            os.close(directory_fd)
        return ref

    def resolve_evidence(self, ref: str, scope: RunScope) -> FileEvidence:
        if scope != self._scope:
            raise PermissionError("evidence scope mismatch")
        match = _REFERENCE.fullmatch(ref)
        if match is None:
            raise ValueError("invalid evidence reference")
        scope_hash, run_hash, candidate_hash, payload_hash = match.groups()
        if scope_hash != self._scope_digest:
            raise PermissionError("evidence reference scope mismatch")
        directory_fd = _open_directory(self._evidence_root, self._evidence_identity)
        try:
            fd = os.open(
                _hash(ref.encode()) + ".json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError("evidence is not a regular file")
                payload = handle.read(_MAX_EVIDENCE_BYTES + 1)
        finally:
            os.close(directory_fd)
        if len(payload) > _MAX_EVIDENCE_BYTES or _hash(payload) != payload_hash:
            raise ValueError("evidence digest mismatch")
        evidence = FileEvidence.model_validate_json(payload)
        if (evidence.scope_digest, evidence.run_digest, evidence.candidate_digest) != (
            scope_hash,
            run_hash,
            candidate_hash,
        ):
            raise ValueError("evidence identity mismatch")
        return evidence
