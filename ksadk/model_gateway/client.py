"""The single model transport used by Studio and SDK runtimes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal

from ksadk._async_iterators import close_async_iterator
from ksadk._model_input import transformation_summary
from ksadk.model_gateway.events import (
    ModelStreamEvent,
    ModelStreamEventType,
    classify_gateway_error,
    safe_gateway_error_code,
)
from ksadk.model_proxy.compat import CompatProfile
from ksadk.studio.contracts import NetworkPolicy, ResolvedModel
from ksadk.studio.model_client import OpenAICompatibleModelClient
from ksadk.studio.model_wire_failure import response_error_usage


class ModelGatewayClient(OpenAICompatibleModelClient):
    """OpenAI-compatible gateway client with a canonical stream vocabulary.

    ``complete`` and ``stream`` remain source-compatible with the former
    Studio client.  New consumers should prefer ``stream_events`` so provider
    wire details never become part of Harness or UI contracts.
    """

    def __init__(self, *args: Any, capability_store: Any | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if capability_store is None:
            from ksadk.harness.model_capability import (
                ModelCapabilityStore,
                global_capability_store,
            )

            capability_store = global_capability_store() or ModelCapabilityStore()
        self.capability_store = capability_store

    @staticmethod
    def _gateway_id(model: ResolvedModel) -> str:
        return str(
            model.metadata.get("gateway_id")
            or model.metadata.get("gatewayId")
            or model.metadata.get("owned_by")
            or model.provider
        ).strip()

    def _register_platform_metadata(self, model: ResolvedModel) -> None:
        if not model.metadata:
            return
        from ksadk.harness.model_capability import apply_gateway_metadata

        apply_gateway_metadata(
            {
                "source": "resolved-model-profile",
                "models": [{"id": model.model, "metadata": dict(model.metadata)}],
            },
            self.capability_store,
            gateway_id=self._gateway_id(model),
        )

    def supports_streaming(self, model: ResolvedModel, *, requested: bool = True) -> bool:
        self._register_platform_metadata(model)
        from ksadk.harness.model_capability import resolve_streaming_mode

        return resolve_streaming_mode(
            requested=requested,
            model_id=model.model,
            store=self.capability_store,
            allow_upgrade=False,
            gateway_id=self._gateway_id(model),
        )

    async def complete(self, model: ResolvedModel, **kwargs: Any):
        self._register_platform_metadata(model)
        return await super().complete(model, **kwargs)

    async def stream(self, model: ResolvedModel, **kwargs: Any):
        self._register_platform_metadata(model)
        iterator = super().stream(model, **kwargs)
        try:
            async for chunk in iterator:
                yield chunk
        finally:
            await close_async_iterator(iterator, owner="model_gateway.transport")

    async def stream_events(
        self,
        model: ResolvedModel,
        *,
        messages: list[dict[str, Any]],
        network_policy: NetworkPolicy,
        timeout_seconds: float,
        tools: list[dict[str, Any]] | None = None,
        max_output_tokens: int | None = None,
        output_budget_mode: Literal["adaptive", "fixed"] = "adaptive",
        reasoning_effort: str | None = None,
        retry_empty_stream: bool = True,
        tool_choice: str | dict[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
        output_token_parameter: Literal["max_tokens", "max_completion_tokens"] | None = None,
        compatibility_profile: CompatProfile | None = None,
        thinking_mode: Literal["enabled", "disabled"] | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        sequence = 0
        last_provider_request_id = ""
        completed = False
        self._register_platform_metadata(model)

        def event(kind: ModelStreamEventType, **kwargs: Any) -> ModelStreamEvent:
            nonlocal sequence
            sequence += 1
            return ModelStreamEvent(kind, model.model, sequence, **kwargs)

        yield event(ModelStreamEventType.TURN_STARTED)
        iterator = self.stream(
            model,
            messages=messages,
            network_policy=network_policy,
            timeout_seconds=timeout_seconds,
            tools=tools,
            max_output_tokens=max_output_tokens,
            **({"thinking_mode": thinking_mode} if thinking_mode is not None else {}),
            **({"output_budget_mode": output_budget_mode}
               if output_budget_mode != "adaptive" else {}),
            reasoning_effort=reasoning_effort,
            retry_empty_stream=retry_empty_stream,
            **({"tool_choice": tool_choice} if tool_choice is not None else {}),
            **({"parallel_tool_calls": parallel_tool_calls}
               if parallel_tool_calls is not None else {}),
            **({"output_token_parameter": output_token_parameter}
               if output_token_parameter is not None else {}),
            **({"compatibility_profile": compatibility_profile} if compatibility_profile else {}),
        )
        try:
            async for chunk in iterator:
                if chunk.provider_request_id:
                    last_provider_request_id = chunk.provider_request_id
                if chunk.provider_input is not None:
                    yield event(
                        ModelStreamEventType.INPUT_TRANSFORMATIONS,
                        provider_input=chunk.provider_input,
                        provider_request_id=chunk.provider_request_id,
                        metadata=transformation_summary(chunk.provider_input.transformations_json),
                    )
                if chunk.text:
                    yield event(
                        ModelStreamEventType.TEXT_DELTA,
                        text=chunk.text,
                        provider_request_id=chunk.provider_request_id,
                    )
                if chunk.public_summary:
                    yield event(ModelStreamEventType.PUBLIC_SUMMARY_DELTA,
                                text=chunk.public_summary,
                                provider_request_id=chunk.provider_request_id)
                if chunk.reasoning:
                    yield event(
                        ModelStreamEventType.REASONING_DELTA,
                        text=chunk.reasoning,
                        provider_request_id=chunk.provider_request_id,
                    )
                for call in chunk.tool_calls:
                    yield event(
                        ModelStreamEventType.TOOL_CALL_COMPLETED,
                        call_id=call.id,
                        tool_name=call.name,
                        arguments=call.arguments,
                        provider_request_id=chunk.provider_request_id,
                    )
                if chunk.usage is not None and chunk.usage.reported:
                    yield event(
                        ModelStreamEventType.USAGE_UPDATED,
                        usage={
                            "input_tokens": max(0, int(chunk.usage.input_tokens)),
                            "output_tokens": max(0, int(chunk.usage.output_tokens)),
                            "cached_tokens": max(0, int(chunk.usage.cached_input_tokens)),
                            "reasoning_tokens": max(0, int(chunk.usage.reasoning_output_tokens)),
                        },
                        provider_request_id=chunk.provider_request_id,
                    )
                if chunk.done:
                    yield event(
                        ModelStreamEventType.TURN_COMPLETED,
                        provider_request_id=chunk.provider_request_id,
                        provider_content=chunk.provider_content,
                        metadata={"finish_reason": chunk.finish_reason}
                        if chunk.finish_reason
                        else {},
                    )
                    completed = True
            # Defensive completion for providers which close without an
            # explicit terminal SSE item.
            if not completed:
                yield event(ModelStreamEventType.TURN_COMPLETED)
        except Exception as exc:
            from ksadk.harness.model_provider import model_failure_diagnostic, model_failure_source

            category, status_code, retry_after_ms = classify_gateway_error(exc)
            source = model_failure_source(exc)
            details = getattr(source, "details", None)
            response_usage = response_error_usage(source)
            if response_usage is not None and response_usage.reported:
                yield event(
                    ModelStreamEventType.USAGE_UPDATED,
                    usage={
                        "input_tokens": response_usage.input_tokens,
                        "output_tokens": response_usage.output_tokens,
                        "cached_tokens": response_usage.cached_input_tokens,
                        "reasoning_tokens": response_usage.reasoning_output_tokens,
                    },
                    provider_request_id=last_provider_request_id,
                )
            gateway_error_code = safe_gateway_error_code(exc)
            finish_reason = details.get("finishReason") if isinstance(details, dict) else None
            safe_finish_reason = (
                finish_reason
                if isinstance(finish_reason, str)
                and finish_reason
                in {"length", "max_tokens", "max_output_tokens", "output_limit", "incomplete"}
                else None
            )
            provider_request_id = (
                str(details.get("providerRequestId") or "")
                if isinstance(details, dict)
                else last_provider_request_id
            )
            if not provider_request_id:
                provider_request_id = last_provider_request_id
            kind = (
                ModelStreamEventType.PROVIDER_RATE_LIMITED
                if category == "rate_limit"
                else ModelStreamEventType.TURN_FAILED
            )
            yield event(
                kind,
                failure_category=category,
                gateway_error_code=gateway_error_code,
                status_code=status_code,
                retry_after_ms=retry_after_ms,
                provider_request_id=provider_request_id,
                metadata={
                    "failure_diagnostic": model_failure_diagnostic(exc),
                    **({"finish_reason": safe_finish_reason} if safe_finish_reason else {}),
                },
            )
            raise
        finally:
            await close_async_iterator(iterator, owner="model_gateway.events")


__all__ = ["ModelGatewayClient"]
