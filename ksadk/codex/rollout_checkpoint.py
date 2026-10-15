"""Bounded native rollout transport for managed Codex recovery.

This copies only one verified session record, never a CODEX_HOME, credential,
configuration, or native database. It does not resume a turn or enable recovery.
The owning Kernel must persist and authorize the checkpoint before restoring it.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, model_validator

MAX_ROLLOUT_BYTES = 8 * 1024 * 1024


class NativeRolloutCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: int = 1
    thread_id: str
    relative_path: str
    sha256: str
    content: str

    @model_validator(mode="after")
    def validate_record(self):
        if self.schema_version != 1 or str(UUID(self.thread_id)) != self.thread_id:
            raise ValueError("CODEX_ROLLOUT_IDENTITY_INVALID")
        path = PurePosixPath(self.relative_path)
        if (
            path.is_absolute()
            or len(path.parts) < 2
            or path.parts[0] != "sessions"
            or any(part in {".", ".."} for part in path.parts)
            or str(path) != self.relative_path
            or "\\" in self.relative_path
            or not path.name.startswith("rollout-")
            or not path.name.endswith(f"-{self.thread_id}.jsonl")
        ):
            raise ValueError("CODEX_ROLLOUT_PATH_INVALID")
        raw = self.content.encode("utf-8")
        if not raw or len(raw) > MAX_ROLLOUT_BYTES or not raw.endswith(b"\n"):
            raise ValueError("CODEX_ROLLOUT_SIZE_OR_BOUNDARY_INVALID")
        if hashlib.sha256(raw).hexdigest() != self.sha256:
            raise ValueError("CODEX_ROLLOUT_DIGEST_MISMATCH")
        rows = [json.loads(line) for line in self.content.splitlines()]
        if any(
            not isinstance(row, dict) or not isinstance(row.get("payload"), dict) for row in rows
        ):
            raise ValueError("CODEX_ROLLOUT_RECORD_INVALID")
        metadata = [row["payload"] for row in rows if row.get("type") == "session_meta"]
        if (
            rows[0].get("type") != "session_meta"
            or len(metadata) != 1
            or metadata[0].get("id") != self.thread_id
        ):
            raise ValueError("CODEX_ROLLOUT_IDENTITY_MISMATCH")
        return self

    def require_completed_turn(self, turn_id: str) -> None:
        """Require the latest native turn's flushed terminal record.

        An interrupted-call snapshot cannot authorize a subsequent user turn.
        Do not accept a completion followed by another turn or native action.
        """
        rows = [json.loads(line) for line in self.content.splitlines()]
        starts = [i for i, row in enumerate(rows) if row.get("type") == "event_msg"
                  and row["payload"].get("type") == "task_started"]
        if not starts or rows[starts[-1]]["payload"].get("turn_id") != turn_id:
            raise ValueError("CODEX_ROLLOUT_COMPLETED_TURN_CONFLICT")
        completed = [i for i, row in enumerate(rows) if row.get("type") == "event_msg"
                     and row["payload"].get("type") == "task_complete"
                     and row["payload"].get("turn_id") == turn_id]
        if not completed:
            raise ValueError("CODEX_ROLLOUT_COMPLETION_MISSING")
        if len(completed) != 1 or completed[0] <= starts[-1]:
            raise ValueError("CODEX_ROLLOUT_COMPLETED_TURN_CONFLICT")
        for row in rows[completed[0] + 1:]:
            if row.get("type") == "token_usage_record":
                continue
            if row.get("type") == "event_msg" and row["payload"].get("type") == "token_count":
                continue
            raise ValueError("CODEX_ROLLOUT_COMPLETED_TURN_CONFLICT")

    def require_call(self, *, turn_id: str, call_id: str, tool: str, arguments: dict) -> None:
        """Check the persisted native call within its original turn, not a UI echo."""
        current_turn = None
        matches = []
        for line in self.content.splitlines():
            row = json.loads(line)
            payload = row["payload"]
            if row.get("type") == "turn_context":
                current_turn = payload.get("turn_id")
            if (
                row.get("type") == "response_item"
                and payload.get("type") == "function_call"
                and payload.get("call_id") == call_id
            ):
                matches.append((current_turn, payload))
        if not matches:
            raise ValueError("CODEX_ROLLOUT_CALL_MISSING")
        if len(matches) != 1:
            raise ValueError("CODEX_ROLLOUT_CALL_AMBIGUOUS")
        native_turn, payload = matches[0]
        native_arguments = json.loads(payload.get("arguments", "null"))
        if (
            native_turn != turn_id
            or payload.get("name") != tool
            or json.dumps(native_arguments, sort_keys=True, allow_nan=False)
            != json.dumps(arguments, sort_keys=True, allow_nan=False)
        ):
            raise ValueError("CODEX_ROLLOUT_CALL_MISMATCH")

    def function_output(self, call_id: str) -> str | None:
        """Return one persisted native output; ambiguous output is never replayable."""
        matches = []
        for line in self.content.splitlines():
            row = json.loads(line)
            payload = row["payload"]
            if (
                row.get("type") == "response_item"
                and payload.get("type") == "function_call_output"
                and payload.get("call_id") == call_id
            ):
                matches.append(payload.get("output"))
        if not matches:
            return None
        if len(matches) != 1 or not isinstance(matches[0], str):
            raise ValueError("CODEX_ROLLOUT_OUTPUT_AMBIGUOUS")
        return matches[0]


@contextmanager
def _parent(home: Path, relative: str, *, create: bool):
    """Walk beneath the caller-owned home without following child symlinks."""
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = PurePosixPath(relative).parts
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def _read(parent_fd: int, name: str) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("CODEX_ROLLOUT_NOT_REGULAR")
        return source.read(MAX_ROLLOUT_BYTES + 1)


def capture_rollout(home: Path, native_path: Path, *, thread_id: str) -> NativeRolloutCheckpoint:
    # Do not resolve the child path: doing so would hide a symlink traversal.
    home = Path(home).absolute()
    relative = Path(native_path).absolute().relative_to(home).as_posix()
    path = PurePosixPath(relative)
    if not path.parts or path.parts[0] != "sessions" or ".." in path.parts:
        raise ValueError("CODEX_ROLLOUT_PATH_INVALID")
    with _parent(home, relative, create=False) as (fd, name):
        raw = _read(fd, name)
    return NativeRolloutCheckpoint(
        thread_id=thread_id,
        relative_path=relative,
        sha256=hashlib.sha256(raw).hexdigest(),
        content=raw.decode("utf-8"),
    )


def restore_rollout(home: Path, checkpoint: NativeRolloutCheckpoint) -> Path:
    # Revalidate even model_construct/model_copy objects supplied internally.
    checkpoint = NativeRolloutCheckpoint.model_validate(checkpoint.model_dump())
    home = Path(home).absolute()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = checkpoint.content.encode("utf-8")
    with _parent(home, checkpoint.relative_path, create=True) as (fd, name):
        temp_name = ".rollout-" + uuid4().hex
        temp_fd = os.open(
            temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
        )
        try:
            with os.fdopen(temp_fd, "wb") as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            try:
                os.link(temp_name, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            except FileExistsError:
                if _read(fd, name) != raw:
                    raise ValueError("CODEX_ROLLOUT_EXISTING_CONFLICT") from None
            os.fsync(fd)
        finally:
            os.unlink(temp_name, dir_fd=fd)
    return home / checkpoint.relative_path


def restore_recovery_rollout(
    home: Path, checkpoint: NativeRolloutCheckpoint, *, call_ids: tuple[str, ...]
) -> NativeRolloutCheckpoint:
    """Retain verified local output written after the durable snapshot.

    A newer model turn or native action is not an output acknowledgement. Such
    progress must be reconciled explicitly, never overwritten or replayed here.
    """
    checkpoint = NativeRolloutCheckpoint.model_validate(checkpoint.model_dump())
    home = Path(home).absolute()
    target = home / checkpoint.relative_path
    try:
        existing = capture_rollout(home, target, thread_id=checkpoint.thread_id)
    except FileNotFoundError:
        restore_rollout(home, checkpoint)
        return checkpoint
    if existing == checkpoint:
        return existing
    if not existing.content.startswith(checkpoint.content):
        raise ValueError("CODEX_ROLLOUT_EXISTING_CONFLICT")
    saved_rows = [json.loads(line) for line in checkpoint.content.splitlines()]
    known_turns = {
        row["payload"].get("turn_id") for row in saved_rows if row.get("type") == "turn_context"
    }
    suffix = existing.content[len(checkpoint.content) :]
    for line in suffix.splitlines():
        row = json.loads(line)
        payload = row["payload"]
        if (
            row.get("type") == "response_item"
            and payload.get("type") == "function_call_output"
            and payload.get("call_id") in call_ids
        ):
            continue
        if (
            row.get("type") == "event_msg"
            and payload.get("type") in {"token_count", "turn_aborted", "task_complete"}
            and (
                payload.get("turn_id") in known_turns
                or (payload.get("type") == "token_count" and "turn_id" not in payload)
            )
        ):
            continue
        if (
            row.get("type") == "token_usage_record"
            and payload.get("thread_id") == checkpoint.thread_id
            and payload.get("turn_id") in known_turns
        ):
            continue
        raise ValueError("CODEX_ROLLOUT_UNCHECKPOINTED_PROGRESS")
    for call_id in call_ids:
        existing.function_output(call_id)
    return existing
