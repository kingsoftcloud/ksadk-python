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
        usage = getattr(response, "usage", None)
        usage_payload = None
        if usage is not None:
            usage_payload = {
                "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            }
        return HarnessReasoningTurn(
            final_text=final_text,
            tool_calls=tuple(calls),
            usage=usage_payload,
        )


__all__ = [
    "HarnessReasoner",
    "HarnessReasoningTurn",
    "HarnessToolCall",
    "LiteLLMHarnessReasoner",
    "resolve_model_identifier",
]
