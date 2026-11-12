"""Same-session background-notification turn admission.

A notification turn is a new run in the *same session* as the source run, inheriting
its background commands and task-chain identity. Unlike a fork (which starts a new
session branch), it stays in the source session so the kernel's ``command_probe``
delivers the background observation at the next model boundary.

The run id is a deterministic function of (scope, source run, task_id), so a repeat
admission (retry or crash recovery) reads the already-created turn instead of
creating a second run. The admitted request is bound by an immutable digest, so a
different request for the same task_id is rejected, not silently treated as
idempotent — mirroring the fork command's request-digest contract.
"""

from __future__ import annotations

import hashlib
import json

from .completion import TaskContract
from .fork import project_session_history
from .ports import RunConfiguration, digest
from .state import (
    NotificationCommand,
    RunScope,
    RunSnapshot,
)


def notification_run_id(scope: RunScope, source_run_id: str, task_id: str) -> str:
    """Deterministic run id for a background-notification admission.

    The same (scope, source run, task) always maps to the same run id, so a repeat
    admission is idempotent at the store level.
    """
    raw = json.dumps(
        ["ksadk.native.notification-turn/v1", scope.model_dump(mode="json"),
         source_run_id, task_id],
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return "wake_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def notification_request_digest(snapshot: RunSnapshot) -> str:
    """Immutable digest of the admitted notification request.

    Run/turn ids and mutable workspace facts do not identify the request.
    Configuration, source authority, task identity and task chain do.
    """
    names = (
        "scope", "notification_command", "session_id", "initial_goal_digest",
        "task_contract", "configuration_digest", "profile_digest", "contract_digest",
        "context_binding", "task_chain_id", "origin",
    )
    payload = snapshot.model_dump(mode="json", include=set(names))
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                     allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def notification_request_command(
    source: RunSnapshot,
    *,
    task_id: str,
    configuration: RunConfiguration,
    contract: TaskContract,
    context_binding: str,
    parent_run_id: str | None = None,
    snapshot_source: RunSnapshot | None = None,
) -> NotificationCommand:
    """Build the notification admission command's request identity.

    The ``request_digest`` binds the immutable admission request (scope, session,
    source, task, configuration, profile, contract, context binding, task chain,
    origin). It is computed from identity fields only — NOT messages or history —
    so it can be evaluated on the existing-run attach path without
    ``project_session_history`` (which requires a terminal parent and would fail
    for a non-terminal head). A changed explicit ``task_contract`` (or any other
    request-identity field) produces a different digest, so a repeat admission
    with a different request is detectable and rejected without rebuilding the
    full snapshot.
    """
    content_source = snapshot_source if snapshot_source is not None else source
    run_id = notification_run_id(source.scope, source.run_id, task_id)
    digest_snapshot = RunSnapshot(
        scope=source.scope,
        run_id=run_id,
        turn_id="notification-request-identity",
        session_id=source.session_id,
        parent_run_id=parent_run_id or source.run_id,
        goal=content_source.goal,
        initial_goal_digest=source.initial_goal_digest,
        messages=(),
        background_commands=content_source.background_commands,
        task_chain_id=content_source.task_chain_id,
        task_progress=content_source.task_progress,
        task_progress_revision=content_source.task_progress_revision,
        task_contract=contract.model_dump(mode="json"),
        context_binding=context_binding,
        profile_digest=configuration.profile.digest,
        contract_digest=digest(contract),
        configuration_digest=configuration.digest,
        origin={
            **source.origin,
            "notification": True,
            "source_run_id": source.run_id,
            "task_id": task_id,
        },
    )
    return NotificationCommand(
        task_id=task_id,
        source_run_id=source.run_id,
        request_digest=notification_request_digest(digest_snapshot),
    )


def build_notification_snapshot(
    source: RunSnapshot,
    *,
    run_id: str,
    turn_id: str,
    task_id: str,
    configuration: RunConfiguration,
    contract: TaskContract,
    context_binding: str,
    workspace_revision: str,
    parent_run_id: str | None = None,
    snapshot_source: RunSnapshot | None = None,
) -> RunSnapshot:
    """Construct a fresh notification turn from the session's current state.

    ``source`` is the ORIGINAL launching run — used for task identity binding
    (``notification_command.source_run_id``) and ``origin``. ``snapshot_source``
    is the current session head (may be a prior notification turn that already
    marked some tasks as ``notified``). If ``snapshot_source`` is None, falls
    back to ``source``.

    The snapshot inherits the current head's ``background_commands`` (carrying
    cumulative ``notified`` state), ``task_chain_id``, ``goal``, and history — so
    batch notifications for multiple tasks don't re-observe already-delivered
    commands. The kernel's ``command_probe`` injects the
    ``background_command_observation`` for only the not-yet-notified commands.
    """
    content_source = snapshot_source if snapshot_source is not None else source
    history = project_session_history(
        content_source, profile_digest=configuration.profile.digest,
        configuration_digest=configuration.digest,
    )
    snapshot = RunSnapshot(
        scope=source.scope,
        run_id=run_id,
        turn_id=turn_id,
        session_id=source.session_id,
        parent_run_id=parent_run_id or source.run_id,
        goal=content_source.goal,
        initial_goal_digest=source.initial_goal_digest,
        messages=history,
        background_commands=content_source.background_commands,
        task_chain_id=content_source.task_chain_id,
        task_progress=content_source.task_progress,
        task_progress_revision=content_source.task_progress_revision,
        task_contract=contract.model_dump(mode="json"),
        context_binding=context_binding,
        profile_digest=configuration.profile.digest,
        contract_digest=digest(contract),
        configuration_digest=configuration.digest,
        origin={
            **source.origin,
            "notification": True,
            "source_run_id": source.run_id,
            "task_id": task_id,
        },
        workspace_revision=workspace_revision if workspace_revision != "unavailable" else None,
        workspace_observation_status=(
            "initial" if workspace_revision != "unavailable" else "unavailable"
        ),
    )
    snapshot = snapshot.model_copy(update={"notification_command": NotificationCommand(
        task_id=task_id,
        source_run_id=source.run_id,
        request_digest=notification_request_digest(snapshot),
    )})
    return snapshot
