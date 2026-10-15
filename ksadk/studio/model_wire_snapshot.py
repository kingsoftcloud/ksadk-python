"""Terminal Responses bodies must agree with already observed stream facts."""

from dataclasses import dataclass

from ksadk.studio.model_buffered_wire import BufferedProjection, buffered_projection
from ksadk.studio.model_responses_content import ResponsesContent
from ksadk.studio.model_wire_identity import invalid_model_frame
from ksadk.studio.model_wire_tools import ResponsesToolCalls, WireTool


@dataclass(frozen=True)
class ResponseSnapshot:
    projection: BufferedProjection
    text_tail: str = ""
    reasoning_tail: str = ""
    completed_calls: tuple[WireTool, ...] = ()


def _tail(observed: str, snapshot: str) -> str:
    if not snapshot.startswith(observed):
        raise invalid_model_frame()
    return snapshot[len(observed):]


def reconcile_responses_snapshot(
    payload: dict, *, status: str, tools: ResponsesToolCalls, text: str, reasoning: str,
    content: ResponsesContent | None = None,
) -> ResponseSnapshot | None:
    if "status" in payload and payload["status"] != status:
        raise invalid_model_frame()
    if "output" not in payload and "output_text" not in payload:
        return None  # Preserve compatibility with terminal metadata-only frames.
    projection = buffered_projection({**payload, "status": status}, wire_api="responses")
    if projection.finish == "content_filter":
        return ResponseSnapshot(projection)
    text_tail, reasoning_tail = (content.reconcile_output(payload["output"])
                                 if content is not None and "output" in payload else ("", ""))
    completed = ()
    if "output" in payload and status == "completed":
        completed = tools.reconcile_output(payload["output"] or [])
    return ResponseSnapshot(
        projection, text_tail + _tail(text + text_tail, projection.content),
        reasoning_tail + _tail(reasoning + reasoning_tail, projection.reasoning)
        if "output" in payload else "", completed,
    )
