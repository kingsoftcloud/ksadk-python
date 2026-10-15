"""Admit trusted Build Kernel starts without importing projected model history."""

from __future__ import annotations

from typing import Any

from ksadk.kernel.state import TERMINAL_RUN_STATES
from ksadk.runtime import StartRequest
from ksadk.studio.contracts import RunStatus
from ksadk.studio.native_user_input import admit_native_user_input


async def admit_native_kernel_start(
    *, spec: Any, request: StartRequest, native: Any, runtime: Any, run_events: Any,
) -> StartRequest:
    """Check immutable Build pins and both trusted host history indexes.

    Ordinary Studio Runs and Kernel Runs have separate host indexes. Neither
    supplies model context to Native: its scoped ledger remains the history
    owner. A ledger head must be accounted for by a compatible host Run before
    continuing, and a prior Managed Run must never be silently imported.
    """
    if request.agent_id != spec.agent_id or not isinstance(request.input, str):
        raise ValueError("native_studio_kernel_identity_unavailable")
    native.admit_studio_build(
        build_id=spec.build_id,
        manifest_sha256=spec.manifest_sha256,
        model=request.model,
        request_config=request.config,
        project_dir=spec.launch_context.project_dir,
        launch_config=spec.launch_context.config,
        require_pin=True,
    )
    normalized_input = admit_native_user_input(request.input, request.input)
    known_heads: set[str] = set()
    for previous in run_events.list_runs(session_id=request.session_id):
        if previous.agent_id != spec.agent_id:
            continue
        if previous.status not in {
            RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED,
            RunStatus.INTERRUPTED, RunStatus.TIMED_OUT,
        }:
            continue
        handle = previous.runtime_handle
        if previous.status is not RunStatus.COMPLETED and not handle.get("run_id"):
            continue
        if (
            previous.build_id != spec.build_id
            or previous.manifest_sha256 != spec.manifest_sha256
            or (handle.get("native_ref") or {}).get("engine_kind") != "native"
        ):
            raise ValueError("native_studio_cross_runtime_history_unavailable")
        known_heads.add(str(handle.get("run_id") or ""))

    instance_id = runtime.config.agent_instance_id
    command_id = str(request.metadata.get("command_id") or "")
    if not command_id:
        raise ValueError("native_studio_kernel_command_unavailable")
    messages = await runtime.kernel_store.list_messages(instance_id, request.session_id)
    for message in messages:
        command = message.command
        if command is None or command.command_type != "enqueue":
            continue
        if str(command.command_id) == command_id:
            continue
        previous = await runtime.kernel_store.find_command_run(
            instance_id, request.session_id, str(command.command_id),
        )
        handle = None if previous is None else previous.metadata.get("handle")
        if not handle:
            continue
        if (
            previous.state not in TERMINAL_RUN_STATES
            or (handle.get("native_ref") or {}).get("engine_kind") != "native"
        ):
            raise ValueError("native_studio_cross_runtime_history_unavailable")
        known_heads.add(str(handle.get("run_id") or ""))

    parent = native.session_parent_run_id(request.session_id)
    if (known_heads and parent not in known_heads) or (not known_heads and parent is not None):
        raise ValueError("native_studio_session_parent_mismatch")
    return request.model_copy(update={
        "metadata": {}, "config": {}, "model": None, "input": normalized_input,
    })
