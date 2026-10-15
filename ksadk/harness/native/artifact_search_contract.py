"""Canonical reserved artifact search contract; no effect owner dependencies."""

import hashlib

from .ports import ToolDefinition

ARTIFACT_SEARCH_NAME = "ArtifactSearch"
ARTIFACT_SEARCH_DEFINITION = ToolDefinition(
    name=ARTIFACT_SEARCH_NAME,
    description=(
        "Search saved complete observations using a literal query and previously returned artifact "
        "URIs. Captured workspace source retains its original path and line numbers. "
        "The query is literal, case-sensitive, and never a shell command or regular expression. "
        "Matches are non-overlapping. complete=false indicates omissions; "
        "inspect that flag before claiming no matches."
    ),
    parameters={
        "type": "object",
        "additionalProperties": False,
        "required": ["uris", "query"],
        "properties": {
            "uris": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
            "query": {"type": "string", "minLength": 1, "maxLength": 1024},
            "max_matches": {"type": "integer", "minimum": 1, "maximum": 16},
            "context_chars": {"type": "integer", "minimum": 0, "maximum": 256},
        },
    },
    read_only=True,
    concurrent_safe=True,
    progress_observation=True,
    context_role="workspace_search",
    feedback_profile_digest="sha256:"
    + hashlib.sha256(b"artifact-search-public-argument-feedback/v1").hexdigest(),
)
