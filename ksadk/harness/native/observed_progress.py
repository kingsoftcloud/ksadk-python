"""Bounded novelty from committed host receipts, never a completion verdict."""

import hashlib
import json

from .ports import ToolDefinition, ToolResult
from .state import RunSnapshot


def advance_observed_progress(
    snapshot: RunSnapshot, result: ToolResult, definition: ToolDefinition | None
) -> dict:
    if (
        definition is None
        or not (definition.read_only or definition.progress_observation)
        or not snapshot.pending_intent
        or result.status != "ok"
        or result.workspace_changed_paths
        or (result.content in (None, "", {}, []) and not result.content_blocks)
    ):
        return {}
    # Prefer a host Port's semantic observation identity. Browser action IDs,
    # capture paths and vision captions can change without a new observation.
    # Legacy readers keep their payload comparison; neither view proves truth
    # or task completion, and tool content cannot supply this host-only field.
    fingerprint = (
        result.progress_observation_digest
        or hashlib.sha256(
            json.dumps(
                [
                    result.content,
                    [block.model_dump(mode="json") for block in result.content_blocks],
                ],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
    )
    seen = snapshot.observed_progress_fingerprints
    if fingerprint in seen or len(seen) >= 2048:
        return {}
    return {
        "observed_progress_revision": snapshot.observed_progress_revision + 1,
        "observed_progress_fingerprints": (*seen, fingerprint),
    }
