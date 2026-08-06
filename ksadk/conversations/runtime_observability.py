from __future__ import annotations

from contextlib import asynccontextmanager, nullcontext
from typing import Any, Mapping, Sequence

from ksadk.sessions import SessionEvent


def _extract_deferred_tool_names(output: Any) -> list[str]:
    if not isinstance(output, Mapping):
        return []
    raw_names = output.get("deferred_tool_names")
    if not isinstance(raw_names, Sequence) or isinstance(raw_names, (str, bytes, bytearray)):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for item in raw_names:
        name = str(item or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _latest_deferred_tool_names(events: Sequence[SessionEvent]) -> list[str]:
    for event in reversed(events):
        if event.event_type != "run_status":
            continue
        metadata = event.metadata or {}
        if metadata.get("detail") != "deferred_tools_selected":
            continue
        return _extract_deferred_tool_names(metadata)
    return []


def _normalize_usage_payload(usage: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(usage, Mapping):
        return {}
    normalized: dict[str, Any] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
    ):
        value = usage.get(key)
        if value is None:
            continue
        try:
            normalized[key] = int(value)
        except (TypeError, ValueError):
            continue
    for key in ("input_token_details", "output_token_details"):
        value = usage.get(key)
        if isinstance(value, Mapping):
            normalized[key] = dict(value)
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, Mapping):
        normalized["prompt_tokens_details"] = dict(prompt_details)
        cached_tokens = prompt_details.get("cached_tokens")
        if cached_tokens is not None:
            try:
                normalized.setdefault("input_token_details", {})["cached"] = int(cached_tokens)
            except (TypeError, ValueError):
                pass
    completion_details = usage.get("completion_tokens_details")
    if isinstance(completion_details, Mapping):
        normalized["completion_tokens_details"] = dict(completion_details)
        reasoning_tokens = completion_details.get("reasoning_tokens")
        if reasoning_tokens is not None:
            try:
                normalized.setdefault("output_token_details", {})["reasoning"] = int(
                    reasoning_tokens
                )
            except (TypeError, ValueError):
                pass
    return normalized


def _usage_from_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        return {}
    return _normalize_usage_payload(metadata.get("usage"))


def _responses_usage_payload(usage: Mapping[str, Any] | None) -> dict[str, Any] | None:
    normalized = _normalize_usage_payload(usage)
    if not normalized:
        return None
    input_tokens = normalized.get("input_tokens", normalized.get("prompt_tokens", 0))
    output_tokens = normalized.get("output_tokens", normalized.get("completion_tokens", 0))
    total_tokens = normalized.get("total_tokens", input_tokens + output_tokens)
    payload = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    if isinstance(normalized.get("input_token_details"), Mapping):
        payload["input_token_details"] = dict(normalized["input_token_details"])
    if isinstance(normalized.get("output_token_details"), Mapping):
        payload["output_token_details"] = dict(normalized["output_token_details"])
    return payload


def _chat_usage_payload(usage: Mapping[str, Any] | None) -> dict[str, Any] | None:
    normalized = _normalize_usage_payload(usage)
    if not normalized:
        return None
    prompt_tokens = normalized.get("prompt_tokens", normalized.get("input_tokens", 0))
    completion_tokens = normalized.get("completion_tokens", normalized.get("output_tokens", 0))
    total_tokens = normalized.get("total_tokens", prompt_tokens + completion_tokens)
    payload = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
    prompt_details = normalized.get("prompt_tokens_details")
    if isinstance(prompt_details, Mapping):
        payload["prompt_tokens_details"] = dict(prompt_details)
    input_token_details = normalized.get("input_token_details")
    if isinstance(input_token_details, Mapping) and input_token_details:
        prompt_details = dict(payload.get("prompt_tokens_details") or {})
        cached_tokens = input_token_details.get("cached")
        if cached_tokens is not None:
            try:
                prompt_details["cached_tokens"] = int(cached_tokens)
            except (TypeError, ValueError):
                pass
        if prompt_details:
            payload["prompt_tokens_details"] = prompt_details
    completion_details = normalized.get("completion_tokens_details")
    if isinstance(completion_details, Mapping):
        payload["completion_tokens_details"] = dict(completion_details)
    output_token_details = normalized.get("output_token_details")
    if isinstance(output_token_details, Mapping) and output_token_details:
        completion_details = dict(payload.get("completion_tokens_details") or {})
        reasoning_tokens = output_token_details.get("reasoning")
        if reasoning_tokens is not None:
            try:
                completion_details["reasoning_tokens"] = int(reasoning_tokens)
            except (TypeError, ValueError):
                pass
        if completion_details:
            payload["completion_tokens_details"] = completion_details
    return payload


def _get_conversation_tracer() -> Any | None:
    try:
        from opentelemetry import trace

        return trace.get_tracer("ksadk.conversations")
    except Exception:
        return None


def _current_span_feedback_metadata() -> dict[str, str]:
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
    except Exception:
        return {}
    return _span_feedback_metadata(span)


def _span_feedback_metadata(span: Any | None) -> dict[str, str]:
    if span is None:
        return {}
    try:
        context = span.get_span_context()
    except Exception:
        return {}
    if not getattr(context, "is_valid", False):
        return {}
    return {
        "trace_id": format(context.trace_id, "032x"),
        "root_span_id": format(context.span_id, "016x"),
    }


def _get_current_span() -> Any | None:
    try:
        from opentelemetry import trace

        return trace.get_current_span()
    except Exception:
        return None


def _span_current_context(span: Any | None):
    if span is None:
        return nullcontext()
    try:
        from opentelemetry.trace import use_span

        return use_span(
            span, end_on_exit=False, record_exception=False, set_status_on_exception=False
        )
    except Exception:
        return nullcontext()


@asynccontextmanager
async def _conversation_span_scope(name: str, *, manual_end: bool = False):
    tracer = _get_conversation_tracer()
    if tracer is None:
        yield None
        return
    if manual_end:
        span = tracer.start_span(name)
        try:
            yield span
        finally:
            try:
                span.end()
            except Exception:
                pass
        return
    span = tracer.start_span(name)
    try:
        yield span
    finally:
        try:
            span.end()
        except Exception:
            pass


def _set_span_attribute(span: Any | None, key: str, value: Any) -> None:
    if span is None:
        return
    if value is None:
        return
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return
    try:
        span.set_attribute(key, value)
    except Exception:
        return


def _set_conversation_input_attributes(span: Any | None, input_text: str | None) -> None:
    text = " ".join(str(input_text or "").split())
    if not text:
        return
    for key in (
        "langfuse.trace.input",
        "langfuse.observation.input",
        "input.value",
        "gen_ai.prompt",
    ):
        _set_span_attribute(span, key, text)


def _set_conversation_output_attributes(span: Any | None, output_text: str | None) -> None:
    text = " ".join(str(output_text or "").split())
    if not text:
        return
    for key in (
        "langfuse.trace.output",
        "langfuse.observation.output",
        "output.value",
        "gen_ai.completion",
    ):
        _set_span_attribute(span, key, text)


def _set_conversation_usage_attributes(
    span: Any | None,
    usage: Mapping[str, Any] | None,
) -> None:
    normalized = _normalize_usage_payload(usage)
    if not normalized:
        return

    def _usage_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    input_tokens = _usage_int(normalized.get("input_tokens"))
    output_tokens = _usage_int(normalized.get("output_tokens"))
    total_tokens = _usage_int(normalized.get("total_tokens") or (input_tokens + output_tokens))
    input_details = normalized.get("input_token_details")
    output_details = normalized.get("output_token_details")
    cache_read_tokens = 0
    reasoning_tokens = 0
    if isinstance(input_details, Mapping):
        cache_read_tokens = _usage_int(
            input_details.get("cache_read")
            or input_details.get("cached")
            or input_details.get("cached_tokens")
        )
    if isinstance(output_details, Mapping):
        reasoning_tokens = _usage_int(
            output_details.get("reasoning") or output_details.get("reasoning_tokens")
        )

    attributes = {
        "gen_ai.usage.input_tokens": input_tokens,
        "gen_ai.usage.output_tokens": output_tokens,
        "gen_ai.usage.total_tokens": total_tokens,
        "llm.usage.prompt_tokens": input_tokens,
        "llm.usage.completion_tokens": output_tokens,
        "llm.usage.total_tokens": total_tokens,
    }
    if cache_read_tokens:
        attributes["gen_ai.usage.cache_read.input_tokens"] = cache_read_tokens
        attributes["llm.usage.cache_read.input_tokens"] = cache_read_tokens
    if reasoning_tokens:
        attributes["gen_ai.usage.reasoning.output_tokens"] = reasoning_tokens
        attributes["llm.usage.reasoning_tokens"] = reasoning_tokens

    for key, value in attributes.items():
        if value:
            _set_span_attribute(span, key, value)


def _set_conversation_span_attributes(
    span: Any,
    *,
    agent_id: str,
    user_id: str,
    session_id: str,
    invocation_id: str,
    runner_name: str,
    model: str | None,
    response_id: str | None = None,
) -> None:
    if span is None:
        return
    try:
        span.set_attribute("ksadk.agent_id", agent_id)
        span.set_attribute("ksadk.user_id", user_id)
        span.set_attribute("ksadk.session_id", session_id)
        span.set_attribute("ksadk.invocation_id", invocation_id)
        span.set_attribute("ksadk.runner", runner_name)
        span.set_attribute("langfuse.trace.name", runner_name)
        span.set_attribute("langfuse.session.id", session_id)
        span.set_attribute("session.id", session_id)
        span.set_attribute("langfuse.user.id", user_id)
        span.set_attribute("user.id", user_id)
        if model:
            span.set_attribute("llm.model_name", model)
            span.set_attribute("gen_ai.request.model", model)
        if response_id:
            span.set_attribute("ksadk.response_id", response_id)
    except Exception:
        return
