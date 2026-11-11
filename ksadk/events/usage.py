"""Apply the canonical usage mode to a run's token counters."""

from collections.abc import Mapping

from ksadk.events.canonical import UsageReported

LANGGRAPH_USAGE_CONTRACT = "model_call_delta_v1"

_USAGE_FIELDS = (
    "input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens",
)


def usage_mode(event: UsageReported) -> str:
    """Interpret the pre-contract LangGraph snapshot producer at read time.

    That runner emitted accumulated values labelled delta. Its persisted v3
    snapshot signature predates both per-call provenance and the explicit
    contract marker. Do not change stored payloads/IDs (retry fingerprints),
    infer semantics from token magnitudes, or reinterpret other producers.
    """
    metadata = event.source.metadata
    if (
        event.mode == "delta"
        and event.source.framework == "langgraph"
        and metadata.get("stream_version") == "v3"
        and metadata.get("channel") == "messages"
        and metadata.get("message_delivery") == "snapshot"
        and metadata.get("seq_semantics") == "source_cursor"
        and metadata.get("graph_run_id") == event.source.native_run_id
        and event.source.native_run_id
        and "usage_contract" not in metadata
        and "usage_origin" not in metadata
    ):
        return "cumulative"
    return event.mode


def apply_usage_count(current: int, sample: int, mode: str = "delta") -> int:
    return current + sample if mode == "delta" else sample


def apply_usage_report(
    current: Mapping[str, int], event: UsageReported
) -> dict[str, int]:
    mode = usage_mode(event)
    return {
        field: apply_usage_count(current.get(field, 0), getattr(event, field), mode)
        for field in _USAGE_FIELDS
    }
