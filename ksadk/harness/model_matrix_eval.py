"""OpenAI-compatible model discovery and Harness compatibility smoke evaluation.

This module deliberately keeps credentials in process memory only.  Reports contain
the endpoint, model identifiers, capability results and token usage, never the API
key.  It is intended for pre-release verification against model gateways exposing
many models behind one ``/v1`` endpoint::

    python -m ksadk.harness.model_matrix_eval \
      --models glm-5.3,kimi-k3,qwen3.8-max --out report.json

Configuration follows the normal Harness contract: ``OPENAI_BASE_URL`` and
``OPENAI_API_KEY``.  The command is an explicit network E2E and is not part of the
default test suite.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence

import httpx

from ksadk.harness.model_provider import (
    classify_model_failure,
    safe_model_error_message,
)
from ksadk.harness.reasoner import HarnessReasoner, LiteLLMHarnessReasoner
from ksadk.harness.tools import HarnessTool


@dataclass(frozen=True)
class DiscoveredModel:
    """Provider-neutral projection of one ``GET /models`` entry."""

    model_id: str
    owned_by: str = ""
    input_modalities: tuple[str, ...] = ()
    context_window: int | None = None


@dataclass(frozen=True)
class ModelMatrixRequirements:
    """Release requirements evaluated without guessing provider capabilities."""

    require_streaming: bool = False
    require_usage: bool = False
    require_overflow: bool = False
    min_context_window: int | None = None
    required_input_modalities: tuple[str, ...] = ()


class StreamingCompatibilityProbe(Protocol):
    """Provider-neutral probe for OpenAI-compatible streaming semantics."""

    async def probe_text(self, *, model: str) -> dict[str, Any]: ...

    async def probe_tool(self, *, model: str, tool: HarnessTool) -> dict[str, Any]: ...


class OpenAICompatibleStreamingProbe:
    """Verify SSE text and fragmented Tool Calling against a real gateway.

    The parser intentionally lives outside the production Reasoner.  This makes the
    matrix capable of detecting provider drift before the Harness starts depending
    on a provider-specific chunk type.  Credentials remain request-only and never
    enter returned reports.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not base_url.strip():
            raise ValueError("OPENAI_BASE_URL is required")
        if not api_key.strip():
            raise ValueError("OPENAI_API_KEY is required")
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._transport = transport
        self._timeout_seconds = timeout_seconds

    async def probe_text(self, *, model: str) -> dict[str, Any]:
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": "只回复 STREAM_MATRIX_OK，不要补充其他内容。",
                }
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        parsed = await self._stream(payload)
        text = "".join(parsed["text_chunks"])
        return {
            "passed": "STREAM_MATRIX_OK" in text,
            "chunk_count": len(parsed["text_chunks"]),
            "finish_reason": parsed["finish_reason"],
            "usage_reported": bool(parsed["usage"]),
        }

    async def probe_tool(self, *, model: str, tool: HarnessTool) -> dict[str, Any]:
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "调用 record_model_matrix_probe 工具，并把 code 精确设置为 KSADK-42。"
                    ),
                }
            ],
            "tools": [tool.openai_schema],
            "tool_choice": "auto",
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        parsed = await self._stream(payload)
        calls = parsed["tool_calls"]
        matched = any(
            call["name"] == tool.name and call["arguments"].get("code") == "KSADK-42"
            for call in calls
        )
        return {
            "passed": matched,
            "tool_call_count": len(calls),
            "fragment_count": parsed["tool_fragment_count"],
            "finish_reason": parsed["finish_reason"],
            "usage_reported": bool(parsed["usage"]),
        }

    async def _stream(self, payload: dict[str, Any]) -> dict[str, Any]:
        text_chunks: list[str] = []
        tool_fragments: dict[int, dict[str, str]] = {}
        usage: dict[str, int] = {}
        finish_reason: str | None = None
        fragment_count = 0
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self._timeout_seconds,
            follow_redirects=True,
        ) as client:
            async with client.stream(
                "POST",
                self._url,
                headers=self._headers,
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError("stream returned malformed SSE JSON") from exc
                    if not isinstance(chunk, dict):
                        raise RuntimeError("stream returned a non-object SSE chunk")
                    raw_usage = chunk.get("usage")
                    if isinstance(raw_usage, dict):
                        usage = {
                            "input_tokens": int(raw_usage.get("prompt_tokens") or 0),
                            "output_tokens": int(raw_usage.get("completion_tokens") or 0),
                        }
                    choices = chunk.get("choices") or []
                    if not isinstance(choices, list):
                        raise RuntimeError("stream returned invalid choices")
                    for choice in choices:
                        if not isinstance(choice, dict):
                            continue
                        if choice.get("finish_reason") is not None:
                            finish_reason = str(choice["finish_reason"])
                        delta = choice.get("delta") or {}
                        if not isinstance(delta, dict):
                            continue
                        content = delta.get("content")
                        if content is not None:
                            text_chunks.append(str(content))
                        for call in delta.get("tool_calls") or []:
                            if not isinstance(call, dict):
                                continue
                            fragment_count += 1
                            index = int(call.get("index") or 0)
                            current = tool_fragments.setdefault(
                                index,
                                {"id": "", "name": "", "arguments": ""},
                            )
                            current["id"] += str(call.get("id") or "")
                            function = call.get("function") or {}
                            if isinstance(function, dict):
                                current["name"] += str(function.get("name") or "")
                                current["arguments"] += str(function.get("arguments") or "")

        tool_calls: list[dict[str, Any]] = []
        for index in sorted(tool_fragments):
            call = tool_fragments[index]
            raw_arguments = call["arguments"] or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                raise RuntimeError("stream emitted invalid JSON tool arguments") from exc
            if not call["name"] or not isinstance(arguments, dict):
                raise RuntimeError("stream emitted an invalid tool call")
            tool_calls.append(
                {
                    "call_id": call["id"] or f"stream-tool-call-{index}",
                    "name": call["name"],
                    "arguments": arguments,
                }
            )
        if not text_chunks and not tool_calls:
            raise RuntimeError("stream completed without text or tool calls")
        return {
            "text_chunks": text_chunks,
            "tool_calls": tool_calls,
            "tool_fragment_count": fragment_count,
            "finish_reason": finish_reason,
            "usage": usage,
        }


class OverflowCompatibilityProbe(Protocol):
    """Provider-neutral probe for context-overflow failure semantics."""

    async def probe_overflow(
        self, *, model: str, context_window: int | None
    ) -> dict[str, Any]: ...


class OpenAICompatibleOverflowProbe:
    """Verify a real gateway classifies context overflow as a recoverable failure.

    发送一个确定超过上下文窗口的请求。合规网关必须以可分类的
    context-length 错误拒绝（``classify_model_failure`` → CONTEXT_LENGTH，
    恢复动作 recover_context），而不是 200 截断、未分类 5xx 或连接重置。
    """

    _FILLER = "上下文溢出探针填充段落 OVERFLOW_MATRIX_PADDING "

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 120.0,
        fallback_context_tokens: int = 200_000,
    ) -> None:
        if not base_url.strip():
            raise ValueError("OPENAI_BASE_URL is required")
        if not api_key.strip():
            raise ValueError("OPENAI_API_KEY is required")
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._fallback_context_tokens = fallback_context_tokens

    async def probe_overflow(
        self, *, model: str, context_window: int | None
    ) -> dict[str, Any]:
        tokens = context_window or self._fallback_context_tokens
        # 保守按 1 token ≈ 2 字符估算，翻倍确保越界（中文多字 1 token）。
        target_chars = int(tokens * 2) + 4096
        filler_chars = len(self._FILLER)
        prompt = self._FILLER * (target_chars // filler_chars + 1)
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt + "\n只回复 OK。",
                }
            ],
            "max_tokens": 8,
        }
        status_code: int | None = None
        detail = ""
        failure_kind = ""
        overflow_detected = False
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout_seconds,
                follow_redirects=True,
            ) as client:
                response = await client.post(
                    self._url,
                    headers=self._headers,
                    json=payload,
                )
            status_code = response.status_code
            if response.status_code == 200:
                detail = "overflow prompt was accepted (provider did not reject)"
            else:
                overflow_detected = True
                error = _ResponseStatusError(response.status_code, response.text[:512])
                detail = safe_model_error_message(error, limit=240)
                failure_kind = classify_model_failure(error).kind.value
        except httpx.HTTPError as exc:
            overflow_detected = True
            detail = safe_model_error_message(exc, limit=240)
            failure_kind = classify_model_failure(exc).kind.value
        return {
            "passed": overflow_detected and failure_kind == "context_length",
            "overflow_detected": overflow_detected,
            "failure_kind": failure_kind,
            "status_code": status_code,
            "detail": detail,
        }


class _ResponseStatusError(Exception):
    """Carry an HTTP status into provider failure classification."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"HTTP {status_code}: {body}")
        self.status_code = status_code


def _models_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/models"


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _model_from_wire(value: dict[str, Any]) -> DiscoveredModel | None:
    model_id = str(value.get("id") or "").strip()
    if not model_id:
        return None
    architecture = value.get("architecture")
    if not isinstance(architecture, dict):
        architecture = {}
    modalities = architecture.get("input_modalities") or value.get("input_modalities") or []
    if isinstance(modalities, str):
        modalities = [modalities]
    if not isinstance(modalities, list):
        modalities = []
    context_window = (
        value.get("context_window")
        or value.get("context_length")
        or value.get("max_context_length")
        or architecture.get("context_window")
    )
    return DiscoveredModel(
        model_id=model_id,
        owned_by=str(value.get("owned_by") or ""),
        input_modalities=tuple(str(item) for item in modalities if str(item).strip()),
        context_window=_positive_int(context_window),
    )


def discover_models(
    *,
    base_url: str,
    api_key: str,
    transport: httpx.BaseTransport | None = None,
    timeout_seconds: float = 30.0,
) -> tuple[DiscoveredModel, ...]:
    """Discover models without persisting or returning the credential."""

    if not base_url.strip():
        raise ValueError("OPENAI_BASE_URL is required")
    if not api_key.strip():
        raise ValueError("OPENAI_API_KEY is required")
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(
        transport=transport,
        timeout=timeout_seconds,
        follow_redirects=True,
    ) as client:
        response = client.get(_models_url(base_url), headers=headers)
        response.raise_for_status()
        payload = response.json()
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("model endpoint returned an invalid /models payload")
    models = [item for row in rows if isinstance(row, dict) if (item := _model_from_wire(row))]
    return tuple(sorted(models, key=lambda item: item.model_id))


def select_models(
    discovered: Sequence[DiscoveredModel],
    requested: Sequence[str],
    *,
    limit: int = 4,
) -> tuple[DiscoveredModel, ...]:
    """Select explicitly requested models, or a deterministic representative sample."""

    by_id = {item.model_id: item for item in discovered}
    names = tuple(dict.fromkeys(name.strip() for name in requested if name.strip()))
    if names:
        missing = [name for name in names if name not in by_id]
        if missing:
            raise ValueError(f"requested model(s) not returned by /models: {missing}")
        return tuple(by_id[name] for name in names)
    return tuple(discovered[: max(1, limit)])


async def _probe_one(
    model: DiscoveredModel,
    *,
    reasoner_factory: Callable[[], HarnessReasoner],
    streaming_probe: StreamingCompatibilityProbe | None,
    overflow_probe: OverflowCompatibilityProbe | None = None,
    requirements: ModelMatrixRequirements,
) -> dict[str, Any]:
    reasoner = reasoner_factory()
    result: dict[str, Any] = {
        "model_id": model.model_id,
        "discovered": True,
        "basic_chat": False,
        "tool_calling": False,
        "usage_reported": False,
        "stream_text": None,
        "stream_tool_calling": None,
        "overflow_classified": None,
        "input_modalities": list(model.input_modalities),
        "context_window": model.context_window,
        "errors": [],
    }
    try:
        turn = await reasoner.complete(
            model=model.model_id,
            prompt="",
            messages=(
                {
                    "role": "user",
                    "content": "只回复 MODEL_MATRIX_OK，不要补充其他内容。",
                },
            ),
            tools=(),
        )
        result["basic_chat"] = "MODEL_MATRIX_OK" in (turn.final_text or "")
        result["usage_reported"] = bool(turn.usage)
        result["usage"] = dict(turn.usage or {})
        if not result["basic_chat"]:
            result["errors"].append("basic_chat_marker_missing")
    except Exception as exc:  # noqa: BLE001 - report provider compatibility
        result["errors"].append(
            f"basic_chat:{type(exc).__name__}:{safe_model_error_message(exc, limit=240)}"
        )

    async def _record_probe(arguments: dict[str, Any], call_id: str | None) -> dict[str, Any]:
        del call_id
        return {"ok": arguments.get("code") == "KSADK-42"}

    tool = HarnessTool(
        name="record_model_matrix_probe",
        description="Record the required compatibility probe code. Always call this tool.",
        parameters={
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
            "additionalProperties": False,
        },
        handler=_record_probe,
        source="model-matrix-eval",
    )
    try:
        turn = await reasoner.complete(
            model=model.model_id,
            prompt="",
            messages=(
                {
                    "role": "user",
                    "content": (
                        "调用 record_model_matrix_probe 工具，并把 code 精确设置为 KSADK-42。"
                    ),
                },
            ),
            tools=(tool,),
        )
        result["tool_calling"] = any(
            call.name == tool.name and call.arguments.get("code") == "KSADK-42"
            for call in turn.tool_calls
        )
        if not result["tool_calling"]:
            result["errors"].append("expected_tool_call_missing")
    except Exception as exc:  # noqa: BLE001 - report provider compatibility
        result["errors"].append(
            f"tool_calling:{type(exc).__name__}:{safe_model_error_message(exc, limit=240)}"
        )
    if streaming_probe is not None:
        try:
            streaming_text = await streaming_probe.probe_text(model=model.model_id)
            result["stream_text"] = bool(streaming_text.get("passed"))
            result["stream_text_details"] = streaming_text
            if not result["stream_text"]:
                result["errors"].append("stream_text_marker_missing")
        except Exception as exc:  # noqa: BLE001 - report provider compatibility
            result["stream_text"] = False
            result["errors"].append(
                f"stream_text:{type(exc).__name__}:{safe_model_error_message(exc, limit=240)}"
            )
        try:
            streaming_tool = await streaming_probe.probe_tool(model=model.model_id, tool=tool)
            result["stream_tool_calling"] = bool(streaming_tool.get("passed"))
            result["stream_tool_details"] = streaming_tool
            if not result["stream_tool_calling"]:
                result["errors"].append("stream_expected_tool_call_missing")
        except Exception as exc:  # noqa: BLE001 - report provider compatibility
            result["stream_tool_calling"] = False
            result["errors"].append(
                f"stream_tool_calling:{type(exc).__name__}:"
                f"{safe_model_error_message(exc, limit=240)}"
            )
    elif requirements.require_streaming:
        result["errors"].append("streaming_probe_not_configured")

    if overflow_probe is not None:
        try:
            overflow = await overflow_probe.probe_overflow(
                model=model.model_id,
                context_window=model.context_window,
            )
            result["overflow_classified"] = bool(overflow.get("passed"))
            result["overflow_details"] = overflow
            if not result["overflow_classified"]:
                result["errors"].append("overflow_failure_not_classified")
        except Exception as exc:  # noqa: BLE001 - report provider compatibility
            result["overflow_classified"] = False
            result["errors"].append(
                f"overflow:{type(exc).__name__}:{safe_model_error_message(exc, limit=240)}"
            )
    elif requirements.require_overflow:
        result["errors"].append("overflow_probe_not_configured")

    if requirements.require_usage and not result["usage_reported"]:
        result["errors"].append("usage_not_reported")

    capability_findings: list[dict[str, Any]] = []
    if requirements.min_context_window is not None:
        actual = model.context_window
        passed = actual is not None and actual >= requirements.min_context_window
        capability_findings.append(
            {
                "capability": "context_window",
                "status": "passed" if passed else "failed",
                "required": requirements.min_context_window,
                "actual": actual,
                "reason": "metadata_missing" if actual is None else "below_minimum",
            }
        )
        if not passed:
            result["errors"].append("required_context_window_unavailable")
    if requirements.required_input_modalities:
        available = set(model.input_modalities)
        missing = [
            item for item in requirements.required_input_modalities if item not in available
        ]
        capability_findings.append(
            {
                "capability": "input_modalities",
                "status": "failed" if missing else "passed",
                "required": list(requirements.required_input_modalities),
                "actual": list(model.input_modalities),
                "missing": missing,
            }
        )
        if missing:
            result["errors"].append("required_input_modality_unavailable")
    result["capability_findings"] = capability_findings
    required_checks = [result["basic_chat"], result["tool_calling"]]
    if streaming_probe is not None or requirements.require_streaming:
        required_checks.extend([result["stream_text"], result["stream_tool_calling"]])
    if overflow_probe is not None or requirements.require_overflow:
        required_checks.append(result["overflow_classified"])
    if requirements.require_usage:
        required_checks.append(result["usage_reported"])
    result["passed"] = all(bool(check) for check in required_checks) and not any(
        item["status"] == "failed" for item in capability_findings
    )
    return result


async def evaluate_model_matrix(
    models: Sequence[DiscoveredModel],
    *,
    reasoner_factory: Callable[[], HarnessReasoner] = LiteLLMHarnessReasoner,
    streaming_probe: StreamingCompatibilityProbe | None = None,
    overflow_probe: OverflowCompatibilityProbe | None = None,
    requirements: ModelMatrixRequirements | None = None,
) -> dict[str, Any]:
    """Run basic chat and tool-calling probes for each selected model."""

    requirements = requirements or ModelMatrixRequirements()
    results = []
    for model in models:
        results.append(
            await _probe_one(
                model,
                reasoner_factory=reasoner_factory,
                streaming_probe=streaming_probe,
                overflow_probe=overflow_probe,
                requirements=requirements,
            )
        )
    passed_count = sum(1 for item in results if item["passed"])
    status = "ready" if results and passed_count == len(results) else "blocked"
    return {
        "schemaVersion": 1,
        "status": status,
        "model_count": len(results),
        "passed_count": passed_count,
        "all_passed": bool(results) and all(item["passed"] for item in results),
        "summary": {
            "passed": passed_count,
            "failed": len(results) - passed_count,
            "skipped": 0,
        },
        "requirements": {
            "require_streaming": requirements.require_streaming,
            "min_context_window": requirements.min_context_window,
            "required_input_modalities": list(requirements.required_input_modalities),
        },
        "results": results,
    }


def run_model_matrix(
    *,
    base_url: str,
    api_key: str,
    requested_models: Sequence[str] = (),
    limit: int = 4,
    include_streaming: bool = False,
    include_overflow: bool = False,
    requirements: ModelMatrixRequirements | None = None,
) -> dict[str, Any]:
    discovered = discover_models(base_url=base_url, api_key=api_key)
    selected = select_models(discovered, requested_models, limit=limit)
    old_base, old_key = os.getenv("OPENAI_BASE_URL"), os.getenv("OPENAI_API_KEY")
    os.environ["OPENAI_BASE_URL"] = base_url
    os.environ["OPENAI_API_KEY"] = api_key
    try:
        streaming_probe = (
            OpenAICompatibleStreamingProbe(base_url=base_url, api_key=api_key)
            if include_streaming
            else None
        )
        overflow_probe = (
            OpenAICompatibleOverflowProbe(base_url=base_url, api_key=api_key)
            if include_overflow
            else None
        )
        evaluated = asyncio.run(
            evaluate_model_matrix(
                selected,
                streaming_probe=streaming_probe,
                overflow_probe=overflow_probe,
                requirements=requirements,
            )
        )
    finally:
        if old_base is None:
            os.environ.pop("OPENAI_BASE_URL", None)
        else:
            os.environ["OPENAI_BASE_URL"] = old_base
        if old_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = old_key
    return {
        "endpoint": base_url,
        "discovered_count": len(discovered),
        "selected_models": [item.model_id for item in selected],
        "streaming_enabled": include_streaming,
        "overflow_enabled": include_overflow,
        **evaluated,
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="ksadk.harness.model_matrix_eval")
    parser.add_argument("--models", default="", help="Comma-separated model identifiers")
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--out", default="")
    parser.add_argument(
        "--streaming",
        action="store_true",
        help="Also verify SSE text and fragmented Tool Calling semantics",
    )
    parser.add_argument(
        "--overflow",
        action="store_true",
        help="Also verify context-overflow failures are classified as context_length",
    )
    args = parser.parse_args()
    base_url = os.getenv("OPENAI_BASE_URL", "")
    api_key = os.getenv("OPENAI_API_KEY", "")
    report = run_model_matrix(
        base_url=base_url,
        api_key=api_key,
        requested_models=args.models.split(","),
        limit=args.limit,
        include_streaming=args.streaming,
        include_overflow=args.overflow,
    )
    safe = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(safe + "\n")
    print(safe)
    raise SystemExit(0 if report["all_passed"] else 1)


if __name__ == "__main__":
    main()


__all__ = [
    "DiscoveredModel",
    "ModelMatrixRequirements",
    "OpenAICompatibleOverflowProbe",
    "OpenAICompatibleStreamingProbe",
    "OverflowCompatibilityProbe",
    "StreamingCompatibilityProbe",
    "discover_models",
    "evaluate_model_matrix",
    "run_model_matrix",
    "select_models",
]
