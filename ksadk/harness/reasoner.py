"""Reasoning boundary used by the Harness runner."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol, Sequence


@dataclass(frozen=True)
class HarnessToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]
    #: 模型给出的参数无法解析时的原因（例如 JSON 截断/损坏）。设置后循环层
    #: 不执行该调用，而是以 invalid_arguments 的 tool result 回灌给模型重试——
    #: 参数形状问题绝不允许升级成整轮致命错误。
    invalid_reason: str | None = None


class HarnessToolCallValidationError(RuntimeError):
    """Provider-neutral validation failure for a model-emitted tool call."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class HarnessReasoningTurn:
    final_text: str | None = None
    tool_calls: tuple[HarnessToolCall, ...] = ()
    #: 本次模型调用的 usage（input_tokens/output_tokens）——引擎据此发 usage.reported。
    usage: dict[str, int] | None = None
    #: 本次模型调用返回的推理文本（reasoning_content）——引擎据此发 reasoning 项。
    reasoning: str | None = None
    #: 可见降级标记（例如"工具回合只返回了思维链"）：由引擎落成事件，不静默。
    degraded: str | None = None


class HarnessReasoner(Protocol):
    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[Any],
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> HarnessReasoningTurn: ...


_MODEL_PROFILE_REF = re.compile(r"^model-profile://(?P<name>[^@/]+)(?:@(?P<version>[^/]+))?$")
_LENGTH_FINISH_REASONS = {"length", "incomplete", "max_tokens", "max_output_tokens"}
_LENGTH_RETRY_TARGET_TOKENS = 16_384


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

    def __init__(
        self,
        *,
        streaming: bool | None = None,
        capability_store: Any | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        # None keeps compatibility while allowing deployments to turn streaming on
        # without changing an immutable Agent Revision.
        self._streaming = streaming
        self._capability_store = capability_store
        self._base_url = str(base_url or "").strip() or None
        self._api_key = str(api_key or "").strip() or None
        #: 最近一次调用实际使用的模式（供审计/测试断言能力裁决结果）。
        self.last_streaming_mode: bool | None = None

    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[Any],
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
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
            # Provider retry is owned by the Harness policy layer so every
            # attempt has a canonical failed event, backoff decision and UI
            # progress item.  Letting LiteLLM retry internally would multiply
            # requests invisibly (especially for 429 responses).
            "num_retries": 0,
        }
        if max_output_tokens is not None:
            if max_output_tokens < 1:
                raise ValueError("max_output_tokens must be positive")
            kwargs["max_tokens"] = max_output_tokens
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        base_url = self._base_url or os.getenv("OPENAI_BASE_URL")
        api_key = self._api_key or os.getenv("OPENAI_API_KEY")
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key
        streaming = self._streaming
        explicit_off = False
        if streaming is None:
            raw = os.getenv("KSADK_MODEL_STREAMING", "").strip().lower()
            streaming = raw in {
                "1",
                "true",
                "yes",
                "on",
            }
            explicit_off = raw != "" and not streaming
        store = self._capability_store
        if store is None:
            from ksadk.harness.model_capability import global_capability_store

            store = global_capability_store()
        if store is not None:
            # 模型兼容策略：矩阵实测声明按模型自动选择受支持的模式——
            # 不支持流式（或流式 Tool Call）的模型降级为非流式；默认
            # 非流式但非流式 Tool Call 不可靠的模型升级为流式。显式
            # 关闭流式（构造参数或环境变量）不升级。
            from ksadk.harness.model_capability import resolve_streaming_mode

            streaming = resolve_streaming_mode(
                requested=streaming,
                model_id=resolved_model,
                store=store,
                allow_upgrade=not explicit_off and self._streaming is None,
            )
        self.last_streaming_mode = streaming
        if streaming:
            kwargs["stream"] = True
            # 严格端点会拒绝未知顶层键（stream_options）：是否携带由声明式
            # 兼容档位决定，而不是无条件发送。
            from ksadk.model_proxy import compat as provider_compat

            if provider_compat.compat_profile(
                base_url=str(self._base_url or os.getenv("OPENAI_BASE_URL") or ""),
                model=str(model),
            ).supports_stream_usage_option:
                kwargs["stream_options"] = {"include_usage": True}
        for recovery_attempt in range(2):
            response = await acompletion(**kwargs)
            try:
                if streaming:
                    return await self._consume_stream(response, model=model)
                return self._consume_response(response, model=model)
            except HarnessToolCallValidationError as exc:
                if (
                    exc.code not in {"model_output_truncated", "model_stream_incomplete"}
                    or recovery_attempt > 0
                ):
                    raise
                if exc.code == "model_output_truncated":
                    kwargs["max_tokens"] = self._expanded_length_budget(kwargs.get("max_tokens"))
        raise AssertionError("unreachable")

    async def stream_complete(
        self,
        *,
        model: str,
        prompt: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[Any],
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式模型调用：逐 chunk yield {"text_delta": str}，
        最后 yield {"turn": HarnessReasoningTurn}。

        与 ``complete(streaming=True)`` 的区别：``complete`` 摈整段返回；
        ``stream_complete`` 逐 chunk 交还调用方（loop 层据此发 TEXT_DELTA 事件），
        显著降低首 token 延迟。
        """
        del prompt
        try:
            from litellm import acompletion
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Harness streaming requires the 'adk' extra (litellm)") from exc

        resolved_model = resolve_model_identifier(model)
        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": list(messages),
            "tools": [tool.openai_schema for tool in tools],
            "tool_choice": "auto",
            "num_retries": 0,
            "stream": True,
        }
        if max_output_tokens is not None:
            if max_output_tokens < 1:
                raise ValueError("max_output_tokens must be positive")
            kwargs["max_tokens"] = max_output_tokens
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        base_url = self._base_url or os.getenv("OPENAI_BASE_URL")
        api_key = self._api_key or os.getenv("OPENAI_API_KEY")
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key
        # 严格端点会拒绝未知顶层键（stream_options）：按兼容档位决定是否携带。
        from ksadk.model_proxy import compat as provider_compat

        if provider_compat.compat_profile(
            base_url=str(base_url or ""), model=str(model)
        ).supports_stream_usage_option:
            kwargs["stream_options"] = {"include_usage": True}

        for recovery_attempt in range(2):
            response = await acompletion(**kwargs)
            if not hasattr(response, "__aiter__"):
                raise RuntimeError(f"Harness model {model!r} returned a non-stream response")

            text_chunks: list[str] = []
            reasoning_chunks: list[str] = []
            tool_fragments: dict[int, dict[str, str]] = {}
            usage_payload: dict[str, int] | None = None
            saw_choice = False
            finish_reasons: set[str] = set()
            buffered_deltas: list[dict[str, Any]] = []
            buffer_until_complete = bool(tools)
            async for chunk in response:
                usage = self._usage_payload(getattr(chunk, "usage", None))
                if usage is not None:
                    usage_payload = usage
                choices = getattr(chunk, "choices", None) or []
                for choice in choices:
                    saw_choice = True
                    finish_reason = str(getattr(choice, "finish_reason", "") or "")
                    if finish_reason:
                        finish_reasons.add(finish_reason)
                    delta = getattr(choice, "delta", None)
                    if delta is None:
                        continue
                    content = getattr(delta, "content", None)
                    if content is not None:
                        text_chunks.append(str(content))
                        item = {"text_delta": str(content)}
                        if buffer_until_complete:
                            buffered_deltas.append(item)
                        else:
                            yield item
                    reasoning_content = getattr(delta, "reasoning_content", None) or getattr(
                        delta, "reasoning", None
                    )
                    if reasoning_content is not None:
                        reasoning_chunks.append(str(reasoning_content))
                        item = {"reasoning_delta": str(reasoning_content)}
                        if buffer_until_complete:
                            buffered_deltas.append(item)
                        else:
                            yield item
                    for call in getattr(delta, "tool_calls", None) or []:
                        index = int(getattr(call, "index", 0) or 0)
                        current = tool_fragments.setdefault(
                            index, {"id": "", "name": "", "arguments": ""}
                        )
                        incoming_id = str(getattr(call, "id", "") or "")
                        if incoming_id:
                            current["id"] = self._merge_stream_call_id(current["id"], incoming_id)
                        function = getattr(call, "function", None)
                        if function is not None:
                            current["name"] += str(getattr(function, "name", "") or "")
                            current["arguments"] += str(getattr(function, "arguments", "") or "")
            if not saw_choice and not usage_payload:
                raise RuntimeError(f"Harness model {model!r} returned an empty stream")
            if self._output_was_truncated(finish_reasons):
                if recovery_attempt == 0 and buffer_until_complete:
                    kwargs["max_tokens"] = self._expanded_length_budget(kwargs.get("max_tokens"))
                    continue
                raise HarnessToolCallValidationError(
                    "model_output_truncated",
                    f"Harness model {model!r} output was truncated before completion",
                )
            # A stream without a terminal finish marker may be a cut SSE
            # connection. Neither parseable tool arguments nor visible text
            # can be promoted to a committed turn without that evidence.
            if not finish_reasons:
                if recovery_attempt == 0 and buffer_until_complete:
                    continue
                raise HarnessToolCallValidationError(
                    "model_stream_incomplete",
                    f"Harness model {model!r} stream ended without a finish marker",
                )
            raw_calls = [
                SimpleToolCall(
                    id=fragment["id"],
                    function=SimpleFunctionCall(
                        name=fragment["name"],
                        arguments=fragment["arguments"] or "{}",
                    ),
                )
                for index, fragment in sorted(tool_fragments.items())
            ]
            calls = self._parse_tool_calls(raw_calls)
            final_text = "".join(text_chunks) or None
            if final_text is None and not calls:
                raise RuntimeError(f"Harness model {model!r} stream produced no content")
            for item in buffered_deltas:
                yield item
            yield {
                "turn": HarnessReasoningTurn(
                    final_text=final_text,
                    tool_calls=calls,
                    usage=usage_payload,
                    reasoning="".join(reasoning_chunks) or None,
                )
            }
            return

    @classmethod
    def _consume_response(cls, response: Any, *, model: str) -> HarnessReasoningTurn:
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise RuntimeError(f"Harness model {model!r} returned no choices")
        finish_reason = str(getattr(choices[0], "finish_reason", "") or "")
        if cls._output_was_truncated({finish_reason}):
            raise HarnessToolCallValidationError(
                "model_output_truncated",
                f"Harness model {model!r} output was truncated before completion",
            )
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
        finish_reasons: set[str] = set()
        async for chunk in response:
            usage = cls._usage_payload(getattr(chunk, "usage", None))
            if usage is not None:
                usage_payload = usage
            choices = getattr(chunk, "choices", None) or []
            for choice in choices:
                saw_choice = True
                finish_reason = str(getattr(choice, "finish_reason", "") or "")
                if finish_reason:
                    finish_reasons.add(finish_reason)
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
                    incoming_id = str(getattr(call, "id", "") or "")
                    if incoming_id:
                        current["id"] = cls._merge_stream_call_id(current["id"], incoming_id)
                    function = getattr(call, "function", None)
                    if function is not None:
                        current["name"] += str(getattr(function, "name", "") or "")
                        current["arguments"] += str(getattr(function, "arguments", "") or "")
        if not saw_choice and not usage_payload:
            raise RuntimeError(f"Harness model {model!r} returned an empty stream")
        if cls._output_was_truncated(finish_reasons):
            raise HarnessToolCallValidationError(
                "model_output_truncated",
                f"Harness model {model!r} output was truncated before completion",
            )
        if not finish_reasons:
            raise HarnessToolCallValidationError(
                "model_stream_incomplete",
                f"Harness model {model!r} stream ended without a finish marker",
            )
        raw_calls = [
            SimpleToolCall(
                id=fragment["id"],
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
    def _output_was_truncated(finish_reasons: set[str]) -> bool:
        return bool(_LENGTH_FINISH_REASONS.intersection(finish_reasons))

    @staticmethod
    def _expanded_length_budget(current: Any) -> int:
        try:
            parsed = int(current) if current is not None else 0
        except (TypeError, ValueError):
            parsed = 0
        return min(max(_LENGTH_RETRY_TARGET_TOKENS, parsed * 2), 1_000_000)

    @staticmethod
    def _merge_stream_call_id(current: str, incoming: str) -> str:
        if not current:
            return incoming
        if incoming == current or current.endswith(incoming):
            return current
        if incoming.startswith(current):
            return incoming
        if current.startswith(("call-", "call_")) and incoming.startswith(("call-", "call_")):
            raise HarnessToolCallValidationError(
                "conflicting_tool_call_id",
                "Harness model stream emitted a conflicting tool call id",
            )
        return current + incoming

    @staticmethod
    def _parse_tool_calls(raw_calls: Sequence[Any]) -> tuple[HarnessToolCall, ...]:
        calls: list[HarnessToolCall] = []
        for call in raw_calls:
            function = getattr(call, "function", None)
            name = str(getattr(function, "name", "") or "").strip()
            raw_arguments = getattr(function, "arguments", None) or "{}"
            invalid_reason: str | None = None
            arguments: Any = {}
            try:
                arguments = (
                    json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                )
            except json.JSONDecodeError:
                # 参数形状问题不中断整轮：标记 invalid_reason，由循环层以
                # invalid_arguments 的 tool result 回灌给模型重试。
                invalid_reason = "model emitted invalid JSON tool arguments"
            call_id = str(getattr(call, "id", "") or "").strip()
            if not call_id:
                raise HarnessToolCallValidationError(
                    "missing_tool_call_id",
                    "Harness model emitted a tool call with missing tool call id",
                )
            if not name:
                raise HarnessToolCallValidationError(
                    "invalid_tool_call", "Harness model emitted an invalid tool call"
                )
            if invalid_reason is None and not isinstance(arguments, dict):
                invalid_reason = "model emitted non-JSON-object tool arguments"
            calls.append(
                HarnessToolCall(
                    call_id=call_id,
                    name=name,
                    arguments=dict(arguments) if isinstance(arguments, dict) else {},
                    invalid_reason=invalid_reason,
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


class ModelGatewayHarnessReasoner:
    """Harness adapter backed by the shared :class:`ModelGatewayClient`.

    This is the default for SDK/direct runtimes.  Studio binds immutable Model
    Profiles to the same client through its provider adapter.  Keeping the
    adapter here (rather than in Studio) avoids making SDK execution depend on
    authoring services while still sharing one HTTP/parser implementation.
    """

    def __init__(
        self,
        *,
        streaming: bool | None = None,
        capability_store: Any | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: int = 120,
        client: Any | None = None,
    ) -> None:
        from ksadk.model_gateway import CredentialResolver, ModelGatewayClient

        resolver = CredentialResolver()
        self._credential_ref = "env://OPENAI_API_KEY"
        if api_key:
            resolver.put_session("OPENAI_API_KEY", api_key)
        self._client = client or ModelGatewayClient(
            credential_resolver=resolver,
            capability_store=capability_store,
        )
        self._streaming = streaming
        self._capability_store = capability_store
        self._base_url = str(base_url or os.getenv("OPENAI_BASE_URL") or "").strip()
        self._timeout_seconds = max(1, int(timeout_seconds))
        self.last_streaming_mode: bool | None = None

    @staticmethod
    def _endpoint(base_url: str, wire_api: str) -> str:
        value = base_url.rstrip("/")
        if not value:
            value = "https://api.openai.com/v1"
        if value.endswith(("/chat/completions", "/responses")):
            return value
        return f"{value}/{'responses' if wire_api == 'responses' else 'chat/completions'}"

    def _resolved_model(self, model: str):
        from ksadk.model_gateway import ModelParameters, ResolvedModel

        resolved = resolve_model_identifier(model)
        # LiteLLM required an ``openai/`` routing prefix.  The gateway wire
        # expects the model id itself, so strip only that compatibility prefix.
        model_id = resolved.removeprefix("openai/")
        wire_api = os.getenv("KSADK_MODEL_WIRE_API", "chat").strip().lower()
        if wire_api not in {"chat", "responses"}:
            wire_api = "chat"
        return ResolvedModel(
            provider="openai-compatible",
            model=model_id,
            endpoint_url=self._endpoint(self._base_url, wire_api),
            credential_ref=self._credential_ref,
            parameters=ModelParameters(),
            wire_api=wire_api,
        )

    def _use_streaming(self, model_id: str) -> bool:
        requested = self._streaming
        explicit_off = requested is False
        if requested is None:
            raw = os.getenv("KSADK_MODEL_STREAMING", "").strip().lower()
            requested = raw in {"1", "true", "yes", "on"} if raw else True
            explicit_off = bool(raw) and not requested
        store = self._capability_store
        if store is None:
            from ksadk.harness.model_capability import global_capability_store

            store = global_capability_store()
        from ksadk.harness.model_capability import resolve_streaming_mode

        selected = resolve_streaming_mode(
            requested=bool(requested),
            model_id=model_id,
            store=store,
            allow_upgrade=not explicit_off and self._streaming is None,
        )
        if hasattr(self._client, "supports_streaming"):
            selected = self._client.supports_streaming(
                self._resolved_model(model_id), requested=selected
            )
        self.last_streaming_mode = selected
        return selected

    @staticmethod
    def _tools(tools: Sequence[Any]) -> list[dict[str, Any]]:
        return [dict(tool.openai_schema) for tool in tools]

    @staticmethod
    def _calls(raw_calls: Sequence[Any]) -> tuple[HarnessToolCall, ...]:
        calls: list[HarnessToolCall] = []
        for call in raw_calls:
            invalid_reason = None
            try:
                arguments = json.loads(call.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
                invalid_reason = "model emitted invalid JSON tool arguments"
            if not isinstance(arguments, dict):
                arguments = {}
                invalid_reason = "model emitted non-JSON-object tool arguments"
            calls.append(
                HarnessToolCall(
                    call_id=call.id,
                    name=call.name,
                    arguments=arguments,
                    invalid_reason=invalid_reason,
                )
            )
        return tuple(calls)

    @staticmethod
    def _usage(usage: Any) -> dict[str, int] | None:
        if usage is None or not getattr(usage, "reported", False):
            return None
        payload = {
            "input_tokens": max(0, int(usage.input_tokens)),
            "output_tokens": max(0, int(usage.output_tokens)),
        }
        cached_tokens = max(0, int(usage.cached_input_tokens))
        reasoning_tokens = max(0, int(usage.reasoning_output_tokens))
        if cached_tokens:
            payload["cached_tokens"] = cached_tokens
        if reasoning_tokens:
            payload["reasoning_tokens"] = reasoning_tokens
        return payload

    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[Any],
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> HarnessReasoningTurn:
        del prompt
        selected = self._resolved_model(model)
        if self._use_streaming(selected.model):
            final: HarnessReasoningTurn | None = None
            async for item in self.stream_complete(
                model=model,
                prompt="",
                messages=messages,
                tools=tools,
                max_output_tokens=max_output_tokens,
                reasoning_effort=reasoning_effort,
            ):
                if "turn" in item:
                    final = item["turn"]
            if final is None:
                raise RuntimeError(f"Harness model {model!r} returned no terminal turn")
            return final
        from ksadk.model_gateway import NetworkPolicy

        response = await self._client.complete(
            selected,
            messages=[dict(message) for message in messages],
            network_policy=NetworkPolicy(mode="open", allow_private_network=True),
            timeout_seconds=self._timeout_seconds,
            max_attempts=1,
            backoff_seconds=0,
            tools=self._tools(tools),
            allow_empty=bool(tools),
            retry_on_length=True,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
        )
        return HarnessReasoningTurn(
            final_text=response.content or None,
            tool_calls=self._calls(response.tool_calls),
            usage=self._usage(response.usage),
            reasoning=response.reasoning or None,
            degraded="reasoning_only_turn" if response.empty_reasoning_only else None,
        )

    async def stream_complete(
        self,
        *,
        model: str,
        prompt: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[Any],
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        del prompt
        selected = self._resolved_model(model)
        if not self._use_streaming(selected.model):
            turn = await self.complete(
                model=model,
                prompt="",
                messages=messages,
                tools=tools,
                max_output_tokens=max_output_tokens,
                reasoning_effort=reasoning_effort,
            )
            yield {"turn": turn}
            return
        from ksadk.model_gateway import NetworkPolicy
        from ksadk.model_gateway.events import ModelStreamEventType

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: list[HarnessToolCall] = []
        usage: dict[str, int] | None = None
        async for event in self._client.stream_events(
            selected,
            messages=[dict(message) for message in messages],
            network_policy=NetworkPolicy(mode="open", allow_private_network=True),
            timeout_seconds=self._timeout_seconds,
            tools=self._tools(tools),
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
        ):
            if event.type is ModelStreamEventType.TEXT_DELTA:
                text_parts.append(event.text)
                yield {"text_delta": event.text}
            elif event.type is ModelStreamEventType.REASONING_DELTA:
                reasoning_parts.append(event.text)
                yield {"reasoning_delta": event.text}
            elif event.type is ModelStreamEventType.TOOL_CALL_COMPLETED:
                invalid_reason = None
                try:
                    arguments = json.loads(event.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                    invalid_reason = "model emitted invalid JSON tool arguments"
                if not isinstance(arguments, dict):
                    arguments = {}
                    invalid_reason = "model emitted non-JSON-object tool arguments"
                calls.append(
                    HarnessToolCall(
                        call_id=event.call_id,
                        name=event.tool_name,
                        arguments=arguments,
                        invalid_reason=invalid_reason,
                    )
                )
            elif event.type is ModelStreamEventType.USAGE_UPDATED:
                values = dict(event.usage or {})
                usage = {
                    "input_tokens": max(0, int(values.get("input_tokens") or 0)),
                    "output_tokens": max(0, int(values.get("output_tokens") or 0)),
                }
                for key in ("cached_tokens", "reasoning_tokens"):
                    value = max(0, int(values.get(key) or 0))
                    if value:
                        usage[key] = value
        yield {
            "turn": HarnessReasoningTurn(
                final_text="".join(text_parts) or None,
                tool_calls=tuple(calls),
                usage=usage,
                reasoning="".join(reasoning_parts) or None,
            )
        }


__all__ = [
    "HarnessReasoner",
    "HarnessReasoningTurn",
    "HarnessToolCall",
    "LiteLLMHarnessReasoner",
    "ModelGatewayHarnessReasoner",
    "resolve_model_identifier",
]
