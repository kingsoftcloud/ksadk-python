"""Reasoning boundary used by the Harness runner."""

from __future__ import annotations

import json
import os
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


class HarnessReasoner(Protocol):
    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[Any],
    ) -> HarnessReasoningTurn: ...


class LiteLLMHarnessReasoner:
    """Use the project's OpenAI-compatible LiteLLM configuration for tool reasoning."""

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

        resolved_model = model if "/" in model else f"openai/{model}"
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
        response = await acompletion(**kwargs)
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise RuntimeError(f"Harness model {model!r} returned no choices")
        message = choices[0].message
        calls: list[HarnessToolCall] = []
        for index, call in enumerate(getattr(message, "tool_calls", None) or []):
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
        content = getattr(message, "content", None)
        final_text = str(content) if content is not None else None
        return HarnessReasoningTurn(final_text=final_text, tool_calls=tuple(calls))


__all__ = [
    "HarnessReasoner",
    "HarnessReasoningTurn",
    "HarnessToolCall",
    "LiteLLMHarnessReasoner",
]
