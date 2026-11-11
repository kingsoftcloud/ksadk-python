"""Distinguish an explicit graph answer from intermediate model messages."""

from collections.abc import Mapping

from ksadk.events.canonical import ItemStarted, RuntimeEvent


def _output_properties(graph):
    try:
        schema = graph.get_output_schema().model_json_schema()
    except Exception:
        # Third-party graph wrappers may not expose a JSON output schema.
        return {}
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    return properties if isinstance(properties, Mapping) else {}


def has_explicit_answer_field(graph) -> bool:
    properties = _output_properties(graph)
    return "output" in properties or "answer" in properties


def uses_explicit_answer_contract(graph) -> bool:
    """Use only the graph's public output schema; unknown shapes stay streaming."""
    properties = _output_properties(graph)
    return "messages" not in properties and ("output" in properties or "answer" in properties)


def internal_model_event(event: RuntimeEvent) -> RuntimeEvent:
    """Keep model traces observable without publishing them as graph answers."""
    if getattr(event, "item_kind", None) not in {"message", "reasoning"}:
        return event
    updates = {
        "source": event.source.model_copy(
            update={
                "metadata": {
                    **event.source.metadata,
                    "conversation_visibility": "internal",
                    "visibility": "internal",
                }
            }
        ),
    }
    if isinstance(event, ItemStarted):
        # Only the later explicit root answer may own final-answer output refs.
        updates["phase"] = "commentary"
    return event.model_copy(update=updates)
