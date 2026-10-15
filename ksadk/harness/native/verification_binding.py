"""Pure identity binding between a completion candidate and execution state."""

from __future__ import annotations

import hashlib
import json

from .state import RunSnapshot


def verification_subject_context_digest(snapshot: RunSnapshot) -> str:
    """Bind verification to the current goal, effects, workspace and artifacts.

    This does not replace a verifier's subject digest. It prevents a report for
    one committed execution context from being accepted after tool effects,
    steering, workspace observation or ArtifactRef identity changes.
    """

    payload = {
        "goal_revision": snapshot.goal_revision,
        "contract_digest": snapshot.contract_digest,
        "tool_calls": snapshot.tool_calls,
        "workspace": {
            "status": snapshot.workspace_observation_status,
            "revision": snapshot.workspace_revision,
            "changed_paths": snapshot.workspace_changed_paths,
            "reason": snapshot.workspace_observation_reason,
            "cursor": snapshot.workspace_observation_cursor,
        },
        "artifacts": snapshot.artifact_refs,
        "last_browser_observation": snapshot.last_browser_observation,
        "last_web_mutation_tool_call": snapshot.last_web_mutation_tool_call,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


__all__ = ["verification_subject_context_digest"]
