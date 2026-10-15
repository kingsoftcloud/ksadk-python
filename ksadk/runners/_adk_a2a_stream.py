"""Preserve wire identity before ADK's converters discard stream semantics."""

from __future__ import annotations

from copy import deepcopy
from functools import partial, wraps
from typing import Any

A2A_STREAM_METADATA = "ksadk_a2a_stream"


def _wrap_converter(converter: Any, *, artifact_update: bool) -> Any:
    if getattr(converter, "_ksadk_stream_identity", False):
        return converter

    @wraps(converter)
    def convert(wire: Any, *args: Any, **kwargs: Any) -> Any:
        event = converter(wire, *args, **kwargs)
        if event is None:
            return None
        from a2a.types import TaskState

        artifacts = [wire.artifact] if artifact_update else list(wire.artifacts)
        observations = []
        for artifact in artifacts:
            converted = event
            if not artifact_update and len(artifacts) > 1:
                single = deepcopy(wire)
                del single.artifacts[:]
                single.artifacts.extend([artifact])
                converted = converter(single, *args, **kwargs)
            parts = getattr(getattr(converted, "content", None), "parts", None) or ()
            terminal = not artifact_update and wire.status.state == TaskState.TASK_STATE_COMPLETED
            observations.append(
                {
                    "task_id": wire.task_id if artifact_update else wire.id,
                    "artifact_id": artifact.artifact_id,
                    "append": bool(wire.append) if artifact_update else False,
                    "last_chunk": bool(wire.last_chunk) if artifact_update else terminal,
                    "snapshot": terminal,
                    "text": {
                        lane: "".join(
                            part.text
                            for part in parts
                            if part.text and bool(part.thought) == (lane == "reasoning")
                        )
                        for lane in ("message", "reasoning")
                    },
                }
            )
        if observations:
            event.custom_metadata = dict(event.custom_metadata or {})
            event.custom_metadata[A2A_STREAM_METADATA] = observations
        return event

    convert._ksadk_stream_identity = True
    return convert


async def _reconcile_terminal_status(
    ctx: Any, response: Any, event: Any, *, task_converter: Any, part_converter: Any
) -> Any:
    """A completed status message summarizes the Task, not another LLM output.

    Use the actual accumulated Task artifacts supplied by ADK's A2A client.
    No association is inferred from the summary's author or text.
    """
    from a2a.types import TaskState, TaskStatusUpdateEvent

    if isinstance(response, tuple):
        task, update = response
        if (
            isinstance(update, TaskStatusUpdateEvent)
            and update.status.state == TaskState.TASK_STATE_COMPLETED
            and task.artifacts
        ):
            converted = task_converter(task, event.author, ctx, part_converter)
            event.custom_metadata = dict(event.custom_metadata or {})
            event.custom_metadata[A2A_STREAM_METADATA] = converted.custom_metadata[
                A2A_STREAM_METADATA
            ]
    return event


def preserve_a2a_stream_identity(agent: Any) -> None:
    """Decorate configured converters once, including nested native agents."""
    seen: set[int] = set()

    def visit(node: Any) -> None:
        if id(node) in seen:
            return
        seen.add(id(node))
        config = getattr(node, "_config", None)
        if callable(getattr(config, "a2a_artifact_update_converter", None)) and callable(
            getattr(config, "a2a_task_converter", None)
        ):
            config.a2a_artifact_update_converter = _wrap_converter(
                config.a2a_artifact_update_converter, artifact_update=True
            )
            config.a2a_task_converter = _wrap_converter(
                config.a2a_task_converter, artifact_update=False
            )
            from google.adk.a2a.agent.config import RequestInterceptor

            if not any(
                getattr(hook.after_request, "_ksadk_terminal_reconcile", False)
                for hook in (config.request_interceptors or ())
            ):
                reconcile = partial(
                    _reconcile_terminal_status,
                    task_converter=config.a2a_task_converter,
                    part_converter=config.a2a_part_converter,
                )
                reconcile._ksadk_terminal_reconcile = True
                config.request_interceptors = [
                    *(config.request_interceptors or ()),
                    RequestInterceptor(after_request=reconcile),
                ]
        for child in getattr(node, "sub_agents", ()):
            visit(child)

    visit(agent)
