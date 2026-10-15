"""Pure, reserved host artifact-reader contract; no adapter or storage effects."""

from .ports import ToolDefinition

ARTIFACT_READ_NAME = "ArtifactRead"
ARTIFACT_READ_DEFINITION = ToolDefinition(
    name=ARTIFACT_READ_NAME,
    description=(
        "Read a bounded character page from a host-owned ArtifactRef supplied with user input "
        "or returned by an earlier tool call in this session. Continue with next_offset_chars "
        "until complete."
    ),
    parameters={
        "type": "object",
        "properties": {
            "uri": {"type": "string", "minLength": 1},
            "offset_chars": {"type": "integer", "minimum": 0},
            "limit_chars": {"type": "integer", "minimum": 1, "maximum": 32000},
        },
        "required": ["uri"],
        "additionalProperties": False,
    },
    read_only=True,
    concurrent_safe=True,
    progress_observation=True,
    context_role="artifact_read",
)
