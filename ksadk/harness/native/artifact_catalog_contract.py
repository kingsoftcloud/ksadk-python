"""Discovery of already earned artifacts without an unbounded prompt inventory."""

from .ports import ToolDefinition

ARTIFACT_CATALOG_POLICY = "earned-artifact-catalog/v1"
ARTIFACT_LIST_NAME = "ArtifactList"
ARTIFACT_LIST_DEFINITION = ToolDefinition(
    name=ARTIFACT_LIST_NAME,
    description=(
        "List previously saved observations available to this Run. Use offset and limit to "
        "retrieve older artifact URIs omitted from the recent context inventory, then use "
        "ArtifactRead or ArtifactSearch. This returns locators, not source contents or proof "
        "that a source was inspected. A page may return fewer than limit to fit the result budget. "
        "Follow next_offset; null means the current inventory is exhausted."
    ),
    parameters={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 8},
        },
    },
    read_only=True,
    concurrent_safe=True,
)
