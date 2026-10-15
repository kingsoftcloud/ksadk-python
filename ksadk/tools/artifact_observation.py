"""Pure model-view framing for an explicitly archived tool observation.

No URI here grants access. The runtime owns the typed reference and verifies
it against the persisted bytes; SDK budget counters only reserve this framing.
"""

from __future__ import annotations

import json

ARCHIVE_MODEL_POLICY = "archived-model-view/v1"
ARCHIVE_SEARCH_MODEL_POLICY = "archived-model-view/v2"
CANONICAL_ARCHIVE_MODEL_POLICY = "archived-model-view/v3"
OBSERVED_ARCHIVE_MODEL_POLICY = "archived-model-view/v4"
SOURCE_ARCHIVE_MODEL_POLICY = "archived-model-view/v5"
ARCHIVE_URI_MAX_CHARS = 256


def archived_observation(
    model_content: str, uri: str | None, *, policy: str = ARCHIVE_MODEL_POLICY
) -> str:
    if policy not in {
        ARCHIVE_MODEL_POLICY, ARCHIVE_SEARCH_MODEL_POLICY, CANONICAL_ARCHIVE_MODEL_POLICY,
        OBSERVED_ARCHIVE_MODEL_POLICY,
        SOURCE_ARCHIVE_MODEL_POLICY,
    }:
        raise ValueError("archive_model_policy_invalid")
    value = json.loads(model_content)
    if not isinstance(value, dict) or value.get("status") != "ok" or "content" not in value:
        raise ValueError("archive_model_view_invalid")
    if {"artifact_uri", "artifact_state", "artifact_instruction"}.intersection(value):
        raise ValueError("archive_model_view_reserved_field")
    if uri is not None and (not uri.startswith("artifact://") or len(uri) > ARCHIVE_URI_MAX_CHARS):
        raise ValueError("archive_model_uri_invalid")
    value.update(
        artifact_state="available" if uri is not None else "unavailable",
        artifact_instruction=(
            (
                "Use ArtifactSearch to query artifact_uri, or ArtifactRead to inspect a page."
                if policy in {
                    ARCHIVE_SEARCH_MODEL_POLICY, CANONICAL_ARCHIVE_MODEL_POLICY,
                    OBSERVED_ARCHIVE_MODEL_POLICY,
                    SOURCE_ARCHIVE_MODEL_POLICY,
                }
                else "Use ArtifactRead with artifact_uri to inspect the saved complete observation."
            )
            if uri is not None
            else "The complete observation could not be saved; only this preview was returned."
        ),
    )
    if uri is not None:
        value["artifact_uri"] = uri
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
