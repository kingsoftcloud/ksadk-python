"""Private adapter checkpoints; writes must not change a Run's control state."""

import json

from ksadk.kernel.errors import InvalidCommandError
from ksadk.kernel.state import RunState

RUNTIME_CHECKPOINT_KEY = "runtime_checkpoint"
MAX_CHECKPOINT_BYTES = 12 * 1024 * 1024


def checkpoint_copy(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("runtime checkpoint must be an object")
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_CHECKPOINT_BYTES:
        raise ValueError("runtime checkpoint is too large")
    return json.loads(encoded)


def require_checkpoint_owner(run, *, agent_instance_id, session_id):
    if run is None or run.agent_instance_id != agent_instance_id or run.session_id != session_id:
        raise InvalidCommandError("runtime checkpoint run identity mismatch")
    if run.state not in {RunState.RUNNING, RunState.WAITING, RunState.PAUSED}:
        raise InvalidCommandError("runtime checkpoint requires an active run")


def preserve_runtime_checkpoint(incoming: dict, existing: dict | None) -> dict:
    """Only the checkpoint writer can replace this reserved private field."""
    result = dict(incoming)
    result.pop(RUNTIME_CHECKPOINT_KEY, None)
    if existing is not None and RUNTIME_CHECKPOINT_KEY in existing:
        result[RUNTIME_CHECKPOINT_KEY] = existing[RUNTIME_CHECKPOINT_KEY]
    return result
