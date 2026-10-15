"""Pure transcript branching; no old execution state or workspace rollback."""

from __future__ import annotations

import hashlib
import json

from pydantic import JsonValue

from .activation_contract import ActivationResourceRef
from .completion import TaskContract
from .history import project_session_history
from .model import ModelReplayMetadata
from .model_replay import remap_anthropic_replay, replay_fields
from .ports import RunConfiguration, digest
from .state import Contract, ForkCommand, ForkSource, RunScope, RunSnapshot


class ForkCreation(Contract):
    """Only the creator may start driving; duplicates receive current facts."""

    snapshot: RunSnapshot
    created: bool


def command_run_id(scope: RunScope, source_run_id: str, command_id: str) -> str:
    raw = json.dumps(
        ["ksadk.native.fork-command/v1", scope.model_dump(mode="json"), source_run_id, command_id],
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return "fork_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def fork_request_digest(snapshot: RunSnapshot) -> str:
    # Run/Turn IDs, remapped historical call IDs and mutable workspace facts
    # do not identify the request. Configuration and source authority do.
    names = (
        "scope", "fork_source", "session_id", "initial_goal_digest", "task_contract",
        "configuration_digest", "profile_digest", "contract_digest", "context_binding",
        "origin", "activation_resource_ref",
    )
    payload = snapshot.model_dump(mode="json", include=set(names))
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                     allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def fork_source(source: RunSnapshot) -> ForkSource:
    return ForkSource(
        source_run_id=source.run_id,
        source_session_id=source.session_id,
        source_revision=source.revision,
        source_event_seq=source.event_seq,
        source_snapshot_digest=digest(source),
    )


def project_fork_history(
    source: RunSnapshot, run_id: str, *, profile_digest: str | None = None,
    configuration_digest: str | None = None,
) -> tuple[dict[str, JsonValue], ...]:
    """Rebuild protocol pair identities while preserving exact source provenance.

    The normal history projector closes unfinished calls as unknown. An
    unchanged host journal retains model frames with historical provenance;
    other imports filter prior controls. Neither grants the new branch rights.
    The new branch gets its own call IDs and matching private replay mappings,
    including those already imported by an earlier source turn.
    """
    messages = project_session_history(
        source, profile_digest=profile_digest, configuration_digest=configuration_digest,
    )
    ids: dict[str, str] = {}
    for message in messages:
        for call in message.get("tool_calls", ()):
            original = call["id"]
            raw = json.dumps([run_id, source.run_id, original], ensure_ascii=False)
            mapped = "fork_" + hashlib.sha256(raw.encode()).hexdigest()[:32]
            ids[original] = mapped
            call["id"] = mapped
    for message in messages:
        if message.get("role") == "assistant" and message.get("provider_replay") is not None:
            message.update(replay_fields(remap_anthropic_replay(
                ModelReplayMetadata.model_validate(message["provider_replay"]), ids,
            )))
        if message.get("role") == "tool":
            message["tool_call_id"] = ids[message["tool_call_id"]]
        elif message.get("kind") == "historical_tool_observation":
            content = message.get("content")
            if not isinstance(content, str):
                continue
            try:
                observation = json.loads(content)
            except ValueError:
                continue
            if (isinstance(observation, dict)
                    and observation.get("observation") == "kernel_effective_tool_input"
                    and isinstance(observation.get("tool_call_id"), str)
                    and observation["tool_call_id"] in ids):
                observation["tool_call_id"] = ids[observation["tool_call_id"]]
                message["content"] = json.dumps(observation, ensure_ascii=False)
    return messages


def build_fork_snapshot(
    source: RunSnapshot,
    *,
    run_id: str,
    turn_id: str,
    session_id: str,
    goal: str,
    configuration: RunConfiguration,
    contract: TaskContract,
    context_binding: str,
    workspace_revision: str,
    activation_resource: ActivationResourceRef | None,
    origin: dict | None,
    command_id: str | None = None,
) -> RunSnapshot:
    """Construct a fresh run from history plus the destination host's authority."""
    snapshot = RunSnapshot(
        scope=source.scope,
        run_id=run_id,
        turn_id=turn_id,
        session_id=session_id,
        fork_source=fork_source(source),
        goal=goal,
        initial_goal_digest=hashlib.sha256(goal.encode("utf-8")).hexdigest(),
        messages=(*project_fork_history(
            source, run_id, profile_digest=configuration.profile.digest,
            configuration_digest=configuration.digest,
        ), {"role": "user", "content": goal}),
        task_contract=contract.model_dump(mode="json"),
        context_binding=context_binding,
        profile_digest=configuration.profile.digest,
        contract_digest=digest(contract),
        configuration_digest=configuration.digest,
        origin=origin or {},
        workspace_revision=workspace_revision if workspace_revision != "unavailable" else None,
        workspace_observation_status=(
            "initial" if workspace_revision != "unavailable" else "unavailable"
        ),
        activation_resource_ref=(
            activation_resource.model_dump(mode="json") if activation_resource is not None else None
        ),
    )
    if command_id is not None:
        snapshot = snapshot.model_copy(update={"fork_command": ForkCommand(
            command_id=command_id, request_digest=fork_request_digest(snapshot),
        )})
    return snapshot
