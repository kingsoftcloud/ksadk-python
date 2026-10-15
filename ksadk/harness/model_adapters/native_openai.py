"""OpenAI-compatible ModelPort with sealed SSE turns and host-owned transport.

HTTP protocol fixtures exercise this adapter; they do not establish model quality
or endpoint compatibility beyond the covered chat protocol fixtures.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from pydantic import ValidationError

from ksadk._async_iterators import owning_async_context
from ksadk._model_json import load_model_json as _load_json
from ksadk._model_reasoning import chat_reasoning
from ksadk._model_refusal import chat_refusal
from ksadk._model_sse import ModelSSEIdleTimeout, model_sse_frames
from ksadk.model_proxy import compat as provider_compat

from ..native.adapters import _project_messages, _snapshot
from ..native.context_history import text_only_tool_message
from ..native.model import (
    ModelFailureKind,
    ModelImageContent,
    ModelResponseKind,
    ModelStreamEvent,
    ModelTextContent,
    ModelTurnResult,
    ModelUsage,
    normalize_response,
)
from ..native.model_replay import seal_model_replay
from ..native.ports import ModelRequest
from ..native.state import RunScope


def _usage(value: object) -> ModelUsage | None:
    if value is None or value == {}:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid usage report")
    # The standard chat usage envelope reports both counters. Do not turn an
    # incomplete report into fabricated zero usage; reject the protocol envelope.
    if "prompt_tokens" not in value or "completion_tokens" not in value:
        raise ValueError("incomplete usage report")
    prompt_details = value.get("prompt_tokens_details")
    completion_details = value.get("completion_tokens_details")
    if prompt_details is None:
        prompt_details = {}
    if completion_details is None:
        completion_details = {}
    if not isinstance(prompt_details, dict) or not isinstance(completion_details, dict):
        raise ValueError("invalid usage details")
    return ModelUsage(
        input_tokens=value["prompt_tokens"],
        output_tokens=value["completion_tokens"],
        cached_input_tokens=prompt_details.get("cached_tokens", 0),
        reasoning_tokens=completion_details.get("reasoning_tokens", 0),
    )


def _tools(value: object) -> tuple[dict[str, object], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("tool_calls must be an array")
    calls = []
    for raw in value:
        if not isinstance(raw, dict) or raw.get("type") != "function":
            raise ValueError("unsupported tool call envelope")
        function = raw.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("arguments"), str):
            raise ValueError("function arguments must be encoded JSON")
        try:
            arguments = _load_json(function["arguments"])
            invalid = not isinstance(arguments, dict)
        except (TypeError, ValueError):
            arguments, invalid = None, True
        calls.append(
            {
                "call_id": raw.get("id"),
                "name": function.get("name"),
                "arguments": {} if invalid else arguments,
                "wire_arguments_invalid": invalid,
            }
        )
    return tuple(calls)


def _result(
    request: ModelRequest,
    kind: ModelResponseKind,
    *,
    finish_reason: str | None = None,
    usage: ModelUsage | None = None,
    text: str = "",
    retry_after_seconds: float | None = None,
    failure_kind: ModelFailureKind | None = None,
) -> ModelTurnResult:
    return ModelTurnResult(
        response_kind=kind,
        request_id=request.request_id,
        attempt_id=request.attempt_id,
        finish_reason=finish_reason,
        usage=usage,
        text=text,
        retry_after_seconds=retry_after_seconds,
        failure_kind=failure_kind,
    )


def _parse_response(
    request: ModelRequest,
    content: bytes,
    *,
    compat_profile: provider_compat.CompatProfile | None = None,
) -> ModelTurnResult:
    finish: str | None = None
    usage: ModelUsage | None = None
    try:
        payload = _load_json(content)
        if not isinstance(payload, dict) or "error" in payload:
            raise ValueError("invalid response envelope")
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise ValueError("expected one completion choice")
        if not choices:
            return _result(request, "empty_response", usage=_usage(payload.get("usage")))
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ValueError("invalid completion choice")
        raw_finish = choice.get("finish_reason")
        if raw_finish is not None and not isinstance(raw_finish, str):
            raise ValueError("invalid finish reason")
        finish = raw_finish
        usage = _usage(payload.get("usage"))
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ValueError("invalid assistant message")
        if chat_refusal(message.get("refusal")):
            finish = "content_filter"
        text = message.get("content")
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise ValueError("unsupported assistant content")
        if compat_profile is not None:
            text = provider_compat.project_inline_reasoning(text, compat_profile)
        raw_calls = message.get("tool_calls")
        try:
            calls = _tools(raw_calls)
            if "function_call" in message:
                raise ValueError("legacy function_call protocol is not supported")
        except (ValueError, TypeError):
            # The normalizer owns precedence: e.g. length/content_filter must
            # still win over a half-built tool batch. {} is a malformed envelope.
            calls = ({},)
        if finish is None and (text.strip() or raw_calls or calls):
            return _result(
                request,
                "provider_retryable_error",
                usage=usage,
                text=text,
                failure_kind="incomplete_response",
            )
        result = normalize_response(
            request_id=request.request_id,
            attempt_id=request.attempt_id,
            finish_reason=finish,
            text=text,
            tool_calls=calls,
            usage=usage,
        )
        return seal_model_replay(result, request.profile, chat_reasoning(message))
    except (ValueError, TypeError, UnicodeError, ValidationError, RecursionError):
        return _result(
            request,
            "provider_terminal_error",
            finish_reason=finish,
            usage=usage,
            failure_kind="invalid_response",
        )


def _native_messages(request: ModelRequest) -> list[dict]:
    """Project host-supplied media without reading files or fetching remote URLs.

    Chat tool messages carry textual receipts. Images are emitted in a user
    observation only after every receipt in the assistant tool batch is present.
    Plain paths stay plain text; only explicit image_url blocks are media.
    """
    projected: list[dict] = []
    pending: set[str] = set()
    images: list[dict] = []
    for message in _project_messages(request):
        role = message["role"]
        if role != "tool" and pending:
            raise ValueError("incomplete assistant tool batch before next message")
        if role == "assistant" and message.get("tool_calls"):
            calls = message["tool_calls"]
            if not isinstance(calls, list):
                raise ValueError("assistant tool calls must be a list")
            ids = [call.get("id") if isinstance(call, dict) else None for call in calls]
            if any(not isinstance(identity, str) or not identity for identity in ids):
                raise ValueError("assistant tool calls require stable IDs")
            if len(set(ids)) != len(ids):
                raise ValueError("duplicate assistant tool IDs")
            pending = set(ids)
        if role == "tool":
            identity = message.get("tool_call_id")
            if identity not in pending:
                raise ValueError("tool receipt does not match the pending assistant batch")
            pending.remove(identity)
        content = message.get("content")
        if isinstance(content, list):
            blocks = []
            for block in content:
                if not isinstance(block, dict):
                    raise ValueError("chat content requires typed blocks")
                model = ModelImageContent if block.get("type") == "image_url" else ModelTextContent
                blocks.append(model.model_validate(block).model_dump(mode="json"))
            media = [block for block in blocks if block["type"] == "image_url"]
            if media and role == "tool":
                text = "\n".join(block["text"] for block in blocks if block["type"] == "text")
                if request.profile.image_input_mode == "text_only":
                    message = text_only_tool_message(message)
                else:
                    message["content"] = text or "Image observation follows the tool batch."
                    images.extend(
                        [
                            {
                                "type": "text",
                                "text": f"Image observation for tool call {identity}:",
                            },
                            *media,
                        ]
                    )
            elif media and role != "user":
                raise ValueError("image observations require user or tool content")
            elif media and request.profile.image_input_mode == "text_only":
                raise ValueError("model profile does not accept user image input")
            else:
                message["content"] = blocks
        elif content is not None and not isinstance(content, str):
            raise ValueError("chat content must be text or typed content blocks")
        projected.append(message)
        if role == "tool" and not pending and images:
            projected.append({"role": "user", "content": images})
            images = []
    if pending:
        raise ValueError("assistant tool batch is missing receipts")
    return projected


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            instant = parsedate_to_datetime(value)
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
            seconds = max(0.0, (instant - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _http_failure(request: ModelRequest, response: httpx.Response) -> ModelTurnResult | None:
    status = response.status_code
    if status in {408, 429} or 500 <= status <= 599:
        return _result(
            request,
            "provider_retryable_error",
            retry_after_seconds=_retry_after(response.headers.get("retry-after")),
            failure_kind=(
                "request_timeout"
                if status == 408
                else "rate_limit"
                if status == 429
                else "server_error"
            ),
        )
    if status == 403:
        return _result(request, "policy_abort")
    if status == 400:
        try:
            body = _load_json(response.content)
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict) and error.get("code") == "context_length_exceeded":
                return _result(request, "context_overflow")
        except (ValueError, TypeError, UnicodeError):
            pass
    if not 200 <= status <= 299:
        return _result(request, "provider_terminal_error", failure_kind="client_error")
    return None


class _StreamTurn:
    def __init__(self, *, capture_replay: bool = False) -> None:
        self.text = ""
        self.capture_replay = capture_replay
        self.reasoning = ""
        self.finish: str | None = None
        self.usage: ModelUsage | None = None
        self.completion_id: str | None = None
        self.tools: dict[int, dict] = {}
        self.malformed_tools = False
        self.refused = False
        self.done = False

    def consume(self, data: str) -> str:
        if data.strip() == "[DONE]":
            self.done = True
            return ""
        payload = _load_json(data)
        if not isinstance(payload, dict) or "error" in payload:
            raise ValueError("invalid stream envelope")
        identity = payload.get("id")
        if identity is not None:
            if not isinstance(identity, str) or not identity:
                raise ValueError("invalid completion ID")
            if self.completion_id is not None and identity != self.completion_id:
                raise ValueError("stream mixed completion identities")
            self.completion_id = identity
        usage = _usage(payload.get("usage"))
        if usage is not None:
            if self.usage is not None and self.usage != usage:
                raise ValueError("conflicting usage reports in one attempt")
            self.usage = usage
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise ValueError("stream requires one choice")
        if not choices:
            return ""
        choice = choices[0]
        if (
            not isinstance(choice, dict)
            or type(choice.get("index")) is not int
            or choice["index"] != 0
            or "message" in choice
        ):
            raise ValueError("snapshot or alternate choice cannot be merged into a delta stream")
        delta = choice.get("delta")
        if delta is None:
            delta = {}
        # Some OpenAI-compatible providers emit role:null on continuation
        # chunks, including tool-call deltas. It means no new role declaration;
        # an actual alternate role must still fail closed.
        if not isinstance(delta, dict) or delta.get("role") not in {None, "assistant"}:
            raise ValueError("invalid stream delta")
        refusal = chat_refusal(delta.get("refusal"))
        if self.finish is not None and refusal:
            raise ValueError("refusal continued after finish")
        self.refused = refusal or self.refused
        content = delta.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise ValueError("invalid visible content delta")
        reasoning = chat_reasoning(delta)
        if self.finish is not None and reasoning:
            raise ValueError("private reasoning continued after finish")
        if self.capture_replay:
            self.reasoning += reasoning
        if self.finish is not None and (content or delta.get("tool_calls")):
            raise ValueError("content continued after finish")
        finish = choice.get("finish_reason")
        if finish is not None:
            if not isinstance(finish, str) or (self.finish is not None and finish != self.finish):
                raise ValueError("conflicting finish reasons")
            self.finish = finish
        self.text += content
        try:
            if "function_call" in delta:
                raise ValueError("legacy function calls are unsupported")
            self._add_tools(delta.get("tool_calls"))
        except (ValueError, TypeError):
            self.malformed_tools = True
        # reasoning_content, reasoning, signatures and provider analysis are
        # private protocol material: never project or concatenate them to text.
        return content

    def _add_tools(self, fragments: object) -> None:
        if fragments is None:
            return
        if not isinstance(fragments, list):
            raise ValueError("tool deltas require a list")
        for fragment in fragments:
            if (
                not isinstance(fragment, dict)
                or type(fragment.get("index")) is not int
                or fragment["index"] < 0
            ):
                raise ValueError("invalid tool fragment index")
            call = self.tools.setdefault(
                fragment["index"],
                {"id": None, "type": None, "function": {"name": "", "arguments": ""}},
            )
            for field in ("id", "type"):
                if field in fragment:
                    value = fragment[field]
                    if (
                        not isinstance(value, str)
                        or not value
                        or (call[field] is not None and call[field] != value)
                    ):
                        raise ValueError("conflicting tool identity")
                    call[field] = value
            function = fragment.get("function", {})
            if not isinstance(function, dict):
                raise ValueError("invalid function delta")
            for field in ("name", "arguments"):
                if field in function:
                    if not isinstance(function[field], str):
                        raise ValueError("function fragment must be text")
                    call["function"][field] += function[field]

    def result(
        self,
        request: ModelRequest,
        error: ModelResponseKind | None = None,
        failure_kind: ModelFailureKind | None = None,
    ) -> ModelTurnResult:
        finish = "content_filter" if self.refused else self.finish
        if error is not None and error != "provider_retryable_error":
            return _result(
                request,
                error,
                finish_reason=finish,
                usage=self.usage,
                text=self.text,
                failure_kind=failure_kind,
            )
        if error == "provider_retryable_error" or not self.done:
            # An observed abort/limit is authoritative even when [DONE] or the
            # usage tail is lost. The shared normalizer preserves those causes
            # before transport incompleteness; successful finishes still become
            # retryable. No unsealed tool fragments cross this boundary.
            result = normalize_response(
                request_id=request.request_id,
                attempt_id=request.attempt_id,
                finish_reason=finish,
                text=self.text,
                stream_complete=False,
                usage=self.usage,
            )
            if result.response_kind == "provider_retryable_error":
                return ModelTurnResult.model_validate(
                    {**result.model_dump(), "failure_kind": failure_kind or "incomplete_response"}
                )
            return result
        if not self.refused and self.finish is None and (
            self.text.strip() or self.tools or self.malformed_tools
        ):
            return _result(
                request,
                "provider_retryable_error",
                finish_reason=finish,
                usage=self.usage,
                text=self.text,
                failure_kind="incomplete_response",
            )
        try:
            if self.malformed_tools:
                raise ValueError("malformed tool fragments")
            calls = _tools([self.tools[index] for index in sorted(self.tools)])
        except (ValueError, TypeError):
            calls = ({},)
        return normalize_response(
            request_id=request.request_id,
            attempt_id=request.attempt_id,
            finish_reason=finish,
            text=self.text,
            tool_calls=calls,
            usage=self.usage,
        )


class OpenAICompatibleModelPort:
    """Perform exactly one HTTP request per model attempt, with optional SSE.

    The host owns authentication, base URL, network policy, TLS, timeout and the
    AsyncClient lifetime. The host must configure transport/auth without hidden
    retries. This adapter creates no client, follows no redirects, and retries
    nothing. Kernel policy owns backoff/budget; Retry-After is bounded there and
    is never disguised as finish_reason. Streaming mode is pinned in the Profile.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        scope: RunScope,
        model: str,
        profile_digest: str,
        streaming: bool = False,
    ) -> None:
        if not model.strip() or not profile_digest.strip():
            raise ValueError("host model mapping and profile digest are required")
        if not client.base_url.is_absolute_url or client.base_url.scheme not in {"http", "https"}:
            raise ValueError("host client requires an absolute HTTP base URL")
        self._client = client
        self._scope = _snapshot(scope)
        self._model = model
        self._profile_digest = profile_digest
        self._compat_profile = provider_compat.compat_profile(
            base_url=str(client.base_url), model=model
        )
        if type(streaming) is not bool:
            raise ValueError("streaming must be a host-selected boolean")
        self._streaming = streaming

    def _request(self, request: ModelRequest, *, streaming: bool) -> tuple[ModelRequest, dict]:
        if request.profile.provider_replay_mode == "anthropic_content":
            raise ValueError("Anthropic replay requires its declared wire adapter")
        sealed = _snapshot(request)
        if sealed.scope != self._scope:
            raise ValueError("model request scope mismatch")
        if sealed.profile.digest != self._profile_digest:
            raise ValueError("model request profile mismatch")
        if sealed.profile.compatibility_digest is not None and (
            sealed.profile.compatibility_digest
            != provider_compat.compat_profile_digest(self._compat_profile)
        ):
            raise ValueError("model compatibility policy differs from the pinned profile")
        if sealed.profile.streaming != streaming:
            raise ValueError("streaming mode differs from the pinned profile")
        payload: dict[str, object] = {
            "model": self._model,
            "messages": _native_messages(sealed),
            "stream": streaming,
            "n": 1,
            sealed.profile.output_token_parameter: sealed.effective_max_output_tokens,
        }
        for field in ("reasoning_effort", "temperature", "top_p", "parallel_tool_calls"):
            value = getattr(sealed.profile, field)
            if value is not None:
                payload[field] = value
        effort = provider_compat.request_reasoning_effort(
            self._compat_profile,
            model=self._model,
            purpose=sealed.purpose,
            requested=sealed.profile.reasoning_effort,
        )
        if effort is not None:
            payload["reasoning_effort"] = effort
        if streaming:
            payload["stream_options"] = {"include_usage": True}
        if sealed.tools:
            payload["tools"] = [
                {"type": "function", "function": tool.function_schema()} for tool in sealed.tools
            ]
            payload["tool_choice"] = (
                provider_compat.project_tool_choice(
                    sealed.effective_tool_choice, self._compat_profile,
                )
            )
        # Keep the direct Port on the same endpoint capability policy as the
        # Studio Gateway. Some profiled reasoning models can spend the whole
        # output budget thinking instead of returning a tool result.
        if provider_compat.request_thinking_disabled(
            self._compat_profile, model=self._model, purpose=sealed.purpose,
            requested=sealed.profile.reasoning_effort,
        ) or provider_compat.should_disable_thinking(
            self._compat_profile,
            tools_present=bool(sealed.tools) and sealed.effective_tool_choice != "none",
            max_output_tokens=sealed.effective_max_output_tokens,
            reasoning_effort=effort,
        ):
            payload = provider_compat.with_thinking_disabled(
                payload, self._compat_profile, reasoning_effort=effort,
            )
        payload = provider_compat.apply_chat_compat(
            payload, self._compat_profile, model=self._model
        )
        return sealed, payload

    @staticmethod
    def _options(request: ModelRequest) -> dict:
        value = request.profile.request_timeout_seconds
        return {"timeout": value} if value is not None else {}

    async def complete(self, request: ModelRequest) -> ModelTurnResult:
        if self._streaming:
            async for event in self.stream(request):
                if event.kind == "final":
                    return event.result
            raise RuntimeError("stream ended without a sealed result")
        sealed, payload = self._request(request, streaming=False)
        try:
            response = await self._client.post(
                "chat/completions",
                json=payload,
                follow_redirects=False,
                **self._options(sealed),
            )
        except (httpx.UnsupportedProtocol, httpx.InvalidURL, httpx.TooManyRedirects):
            return _result(sealed, "provider_terminal_error", failure_kind="client_error")
        except httpx.TimeoutException:
            return _result(sealed, "provider_retryable_error", failure_kind="request_timeout")
        except (httpx.NetworkError, httpx.RemoteProtocolError):
            return _result(sealed, "provider_retryable_error", failure_kind="network_error")
        except httpx.RequestError:
            return _result(sealed, "provider_terminal_error", failure_kind="client_error")
        except Exception:
            # Host auth hooks or transport code may include credentials in
            # exception messages. Expose only the safe terminal classification.
            return _result(sealed, "provider_terminal_error", failure_kind="host_error")
        if error := _http_failure(sealed, response):
            return error
        return _parse_response(sealed, response.content, compat_profile=self._compat_profile)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        sealed, payload = self._request(request, streaming=True)
        turn = _StreamTurn(capture_replay=sealed.profile.provider_replay_mode != "none")
        seq, result = 0, None
        inline_reasoning = provider_compat.InlineReasoningProjector(self._compat_profile)
        try:
            response_context = self._client.stream(
                "POST",
                "chat/completions",
                json=payload,
                follow_redirects=False,
                **self._options(sealed),
            )
            async with owning_async_context(response_context, owner="native_http") as response:
                if not 200 <= response.status_code <= 299:
                    await response.aread()
                    result = _http_failure(sealed, response)
                else:
                    async with model_sse_frames(
                        response.aiter_bytes(), max_bytes=8 * 1024 * 1024,
                        idle_timeout_seconds=(
                            sealed.profile.request_timeout_seconds
                            if sealed.profile.request_timeout_seconds is not None
                            else self._client.timeout.read
                        ),
                    ) as frames:
                        async for frame in frames:
                            if not frame.data.strip():
                                continue
                            delta = turn.consume(frame.data)
                            if delta:
                                public_delta = inline_reasoning.push(delta)
                                if public_delta:
                                    seq += 1
                                    yield ModelStreamEvent(
                                        kind="text_delta",
                                        request_id=sealed.request_id,
                                        attempt_id=sealed.attempt_id,
                                        seq=seq,
                                        text=public_delta,
                                    )
                            if turn.done:
                                break
        except (httpx.UnsupportedProtocol, httpx.InvalidURL, httpx.TooManyRedirects):
            result = turn.result(sealed, "provider_terminal_error", "client_error")
        except (httpx.TimeoutException, ModelSSEIdleTimeout):
            result = turn.result(sealed, "provider_retryable_error", "request_timeout")
        except (httpx.NetworkError, httpx.RemoteProtocolError):
            result = turn.result(sealed, "provider_retryable_error", "network_error")
        except (ValueError, TypeError, UnicodeError, ValidationError, RecursionError):
            result = turn.result(sealed, "provider_terminal_error", "invalid_response")
        except Exception:
            result = turn.result(sealed, "provider_terminal_error", "host_error")
        if result is None:
            result = turn.result(sealed)
        if result.response_kind not in {"provider_retryable_error", "provider_terminal_error"}:
            public_tail = inline_reasoning.seal()
            if public_tail:
                seq += 1
                yield ModelStreamEvent(
                    kind="text_delta",
                    request_id=sealed.request_id,
                    attempt_id=sealed.attempt_id,
                    seq=seq,
                    text=public_tail,
                )
        public_text = provider_compat.project_inline_reasoning(result.text, self._compat_profile)
        if public_text != result.text:
            kind = (
                "empty_response"
                if result.response_kind == "final_candidate" and not public_text.strip()
                else result.response_kind
            )
            result = ModelTurnResult.model_validate(
                {**result.model_dump(), "text": public_text, "response_kind": kind}
            )
        result = seal_model_replay(result, sealed.profile, turn.reasoning)
        yield ModelStreamEvent(
            kind="final",
            request_id=sealed.request_id,
            attempt_id=sealed.attempt_id,
            seq=seq + 1,
            result=result,
        )
