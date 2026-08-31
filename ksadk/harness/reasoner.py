"""Reasoning boundary used by the Harness runner."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol, Sequence


@dataclass(frozen=True)
class HarnessToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class HarnessReasoningTurn:
    final_text: str | None = None
    tool_calls: tuple[HarnessToolCall, ...] = ()
    #: 本次模型调用的 usage（input_tokens/output_tokens）——引擎据此发 usage.reported。
    usage: dict[str, int] | None = None


class HarnessReasoner(Protocol):
    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[Any],
    ) -> HarnessReasoningTurn: ...


_MODEL_PROFILE_REF = re.compile(r"^model-profile://(?P<name>[^@/]+)(?:@(?P<version>[^/]+))?$")


def resolve_model_identifier(model: str) -> str:
    """Resolve a versioned Model Profile reference without putting secrets in a Bundle.

    ``KSADK_MODEL_PROFILE_MAP`` is a JSON object whose keys are immutable
    ``model-profile://...`` references and whose values are provider model identifiers.
    When no explicit mapping is configured, the profile name is used as an
    OpenAI-compatible model identifier. Endpoint and credential resolution remains
    environment/service owned (``OPENAI_BASE_URL`` / ``OPENAI_API_KEY``).
    """

    candidate = model.strip()
    if candidate.startswith("model-profile://"):
        raw_mapping = os.getenv("KSADK_MODEL_PROFILE_MAP", "").strip()
        if raw_mapping:
            try:
                mapping = json.loads(raw_mapping)
            except json.JSONDecodeError as exc:
                raise RuntimeError("KSADK_MODEL_PROFILE_MAP must be valid JSON") from exc
            if not isinstance(mapping, dict):
                raise RuntimeError("KSADK_MODEL_PROFILE_MAP must be a JSON object")
            mapped = mapping.get(candidate)
            if mapped is not None:
                if not isinstance(mapped, str) or not mapped.strip():
                    raise RuntimeError(f"invalid model mapping for {candidate!r}")
                candidate = mapped.strip()
            else:
                match = _MODEL_PROFILE_REF.fullmatch(candidate)
                if match is None:
                    raise RuntimeError(f"invalid Model Profile reference: {candidate!r}")
                candidate = match.group("name")
        else:
            match = _MODEL_PROFILE_REF.fullmatch(candidate)
            if match is None:
                raise RuntimeError(f"invalid Model Profile reference: {candidate!r}")
            candidate = match.group("name")
    return candidate if "/" in candidate else f"openai/{candidate}"


class LiteLLMHarnessReasoner:
    """Use the project's OpenAI-compatible LiteLLM configuration for tool reasoning."""

    def __init__(self, *, streaming: bool | None = None) -> None:
        # None keeps compatibility while allowing deployments to turn streaming on
        # without changing an immutable Agent Revision.
        self._streaming = streaming

    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[Any],
    ) -> HarnessReasoningTurn:
        del prompt
        try:
            from litellm import acompletion
        except ImportError as exc:  # pragma: no cover - depends on optional install
            raise RuntimeError(
                "Harness reasoning requires the 'adk' extra (litellm); "
                "install ksadk[adk] or inject a HarnessReasoner"
            ) from exc

        resolved_model = resolve_model_identifier(model)
        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": list(messages),
            "tools": [tool.openai_schema for tool in tools],
            "tool_choice": "auto",
        }
        base_url = os.getenv("OPENAI_BASE_URL")
        api_key = os.getenv("OPENAI_API_KEY")
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key
        streaming = self._streaming
        if streaming is None:
            streaming = os.getenv("KSADK_MODEL_STREAMING", "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        if streaming:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}
        response = await acompletion(**kwargs)
        if streaming:
            return await self._consume_stream(response, model=model)
        return self._consume_response(response, model=model)

    @classmethod
    def _consume_response(cls, response: Any, *, model: str) -> HarnessReasoningTurn:
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise RuntimeError(f"Harness model {model!r} returned no choices")
        message = choices[0].message
        calls = cls._parse_tool_calls(getattr(message, "tool_calls", None) or [])
        content = getattr(message, "content", None)
        final_text = str(content) if content is not None else None
        return HarnessReasoningTurn(
            final_text=final_text,
            tool_calls=calls,
            usage=cls._usage_payload(getattr(response, "usage", None)),
        )

    @classmethod
    async def _consume_stream(cls, response: Any, *, model: str) -> HarnessReasoningTurn:
        if not hasattr(response, "__aiter__"):
            raise RuntimeError(f"Harness model {model!r} returned a non-stream response")
        text_chunks: list[str] = []
        tool_fragments: dict[int, dict[str, str]] = {}
        usage_payload: dict[str, int] | None = None
        saw_choice = False
        async for chunk in response:
            usage = cls._usage_payload(getattr(chunk, "usage", None))
            if usage is not None:
                usage_payload = usage
            choices = getattr(chunk, "choices", None) or []
            for choice in choices:
                saw_choice = True
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                content = getattr(delta, "content", None)
                if content is not None:
                    text_chunks.append(str(content))
                for call in getattr(delta, "tool_calls", None) or []:
                    index = int(getattr(call, "index", 0) or 0)
                    current = tool_fragments.setdefault(
                        index,
                        {"id": "", "name": "", "arguments": ""},
                    )
                    current["id"] += str(getattr(call, "id", "") or "")
                    function = getattr(call, "function", None)
                    if function is not None:
                        current["name"] += str(getattr(function, "name", "") or "")
                        current["arguments"] += str(
                            getattr(function, "arguments", "") or ""
                        )
        if not saw_choice and not usage_payload:
            raise RuntimeError(f"Harness model {model!r} returned an empty stream")
        raw_calls = [
            SimpleToolCall(
                id=fragment["id"] or f"stream-tool-call-{index}",
                function=SimpleFunctionCall(
                    name=fragment["name"],
                    arguments=fragment["arguments"] or "{}",
                ),
            )
            for index, fragment in sorted(tool_fragments.items())
        ]
        calls = cls._parse_tool_calls(raw_calls)
        final_text = "".join(text_chunks) or None
        if final_text is None and not calls:
            raise RuntimeError(f"Harness model {model!r} stream produced no content")
        return HarnessReasoningTurn(
            final_text=final_text,
            tool_calls=calls,
            usage=usage_payload,
        )

    @staticmethod
    def _parse_tool_calls(raw_calls: Sequence[Any]) -> tuple[HarnessToolCall, ...]:
        calls: list[HarnessToolCall] = []
        for index, call in enumerate(raw_calls):
            function = getattr(call, "function", None)
            name = str(getattr(function, "name", "") or "").strip()
            raw_arguments = getattr(function, "arguments", None) or "{}"
            try:
                arguments = (
                    json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                )
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Harness model emitted invalid JSON arguments for tool {name!r}"
                ) from exc
            if not name or not isinstance(arguments, dict):
                raise RuntimeError("Harness model emitted an invalid tool call")
            calls.append(
                HarnessToolCall(
                    call_id=str(getattr(call, "id", "") or f"tool-call-{index}"),
                    name=name,
                    arguments=dict(arguments),
                )
            )
        return tuple(calls)

    @staticmethod
    def _usage_payload(usage: Any) -> dict[str, int] | None:
        if usage is None:
            return None

        def _value(obj: Any, *names: str) -> int:
            if obj is None:
                return 0
            for name in names:
                raw = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
                if raw is not None:
                    try:
                        return max(0, int(raw))
                    except (TypeError, ValueError):
                        continue
            return 0

        prompt_details = (
            usage.get("prompt_tokens_details")
            if isinstance(usage, dict)
            else getattr(usage, "prompt_tokens_details", None)
        )
        completion_details = (
            usage.get("completion_tokens_details")
            if isinstance(usage, dict)
            else getattr(usage, "completion_tokens_details", None)
        )
        cached = _value(prompt_details, "cached_tokens", "cache_read_input_tokens") or _value(
            usage, "cache_read_input_tokens", "cached_tokens"
        )
        reasoning = _value(completion_details, "reasoning_tokens") or _value(
            usage, "reasoning_tokens"
        )
        input_tokens = _value(usage, "prompt_tokens", "input_tokens")
        output_tokens = _value(usage, "completion_tokens", "output_tokens")
        payload = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        if cached:
            payload["cached_tokens"] = min(cached, input_tokens) if input_tokens else cached
        if reasoning:
            payload["reasoning_tokens"] = (
                min(reasoning, output_tokens) if output_tokens else reasoning
            )
        return payload


@dataclass(frozen=True)
class SimpleFunctionCall:
    """Internal normalized call fragment; never exposed as a public contract."""

    name: str
    arguments: str


@dataclass(frozen=True)
class SimpleToolCall:
    """Internal normalized Tool Call used by the streaming assembler."""

    id: str
    function: SimpleFunctionCall


__all__ = [
    "HarnessReasoner",
    "HarnessReasoningTurn",
    "HarnessToolCall",
    "LiteLLMHarnessReasoner",
    "resolve_model_identifier",
]
