"""Local Studio execution for immutable PluginHost AgentBundle v2 builds."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ksadk.harness.reasoner import (
    HarnessReasoner,
    HarnessReasoningTurn,
    HarnessToolCall,
)
from ksadk.plugins.builtins import (
    builtin_capability_factories,
    builtin_capability_manifests,
    builtin_capability_permissions,
)
from ksadk.plugins.bundle import PluginBundleError, PluginBundleResolver, ResolvedPluginBundle
from ksadk.plugins.contracts import CompositionProfile, PluginManifest
from ksadk.plugins.host import PluginHost, PluginHostError
from ksadk.plugins.providers.codex import CodexTurnResult
from ksadk.plugins.providers.dsh_mcp import (
    DSH_PROFILE_MCP_PLUGIN_ID,
    DshProfileMCPFactory,
    dsh_profile_mcp_manifest,
)
from ksadk.plugins.providers.harness import (
    HarnessEngineSelector,
    HarnessNativeAdapterBuilder,
    HarnessTurnResult,
    KsADKHarnessProviderFactory,
)
from ksadk.plugins.providers.legacy import (
    LegacyBundleAdapter,
    LegacyBundleCompatibilityError,
    LegacyHarnessSource,
)
from ksadk.plugins.providers.legacy_catalog import (
    CODEX_AGENT_PROVIDER_PLUGIN_ID,
    KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID,
    builtin_agent_provider_manifests,
    legacy_harness_agent_provider_manifest,
)
from ksadk.plugins.providers.platform_resources import (
    PLATFORM_RESOURCE_MCP_PLUGIN_ID,
    PLATFORM_RESOURCE_MCP_REF,
    PlatformResourceMCPFactory,
    platform_resource_mcp_manifest,
)
from ksadk.plugins.resolver import PluginRegistry
from ksadk.resource_runtime.policy_authorization import FullAccessResourceWriteAuthorizer
from ksadk.runtime import RuntimeExecutor, RuntimeLaunchContext
from ksadk.sessions.base import BaseSessionService
from ksadk.studio.contracts import (
    BuildRecord,
    NetworkPolicy,
    ResolvedModel,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.model_client import OpenAICompatibleModelClient
from ksadk.studio.native_context_registration import StudioNativeContextRegistration
from ksadk.studio.native_hook_registration import StudioNativeHookRegistration
from ksadk.studio.plugin_kernel_adapter import StudioPluginKernelAdapter
from ksadk.studio.plugin_native_activation import StudioNativeActivationMixin, _NativeActivation
from ksadk.studio.plugin_runtime_admission import RuntimeAdmissionGate
from ksadk.studio.repository import BuildRepository
from ksadk.studio.run_service import StudioRunSpec
from ksadk.studio.workspace import Workspace
from ksadk.tools.gateway import normalize_tool_approval_mode

_COMPOSED_RUNTIME_TYPES = frozenset({"harness", "plugin"})
_DEFAULT_REASONING_ONLY_ACTION_BUDGET_CHARS = 8_192


def _model_request_timeout_seconds(execution: Mapping[str, Any]) -> int:
    """Cap a stalled provider request independently from the complete run budget."""

    run_timeout = int(
        execution.get("timeoutSeconds") or execution.get("timeout_seconds") or 300
    )
    raw_idle_timeout = os.getenv("KSADK_MODEL_REQUEST_IDLE_TIMEOUT_SECONDS")
    default_idle_timeout = min(180, max(60, run_timeout // 10))
    try:
        idle_timeout = (
            int(raw_idle_timeout)
            if raw_idle_timeout is not None
            else default_idle_timeout
        )
    except (TypeError, ValueError):
        idle_timeout = default_idle_timeout
    return max(1, min(run_timeout, max(1, idle_timeout)))


def _model_total_timeout_seconds(execution: Mapping[str, Any]) -> int:
    """整个 run 的模型预算，用作增量传输的硬上限。

    ``_model_request_timeout_seconds`` 是"无进度"超时（可能只有 60s）；增量流在
    持续吐字时不受它约束，所以还需要一个上限，防止一条只发 keepalive 的流把
    整个 run 拖死。
    """

    return max(1, int(execution.get("timeoutSeconds") or execution.get("timeout_seconds") or 300))


def _reasoning_only_action_budget_chars() -> int:
    """Bound a tool-capable turn that produces reasoning but no action.

    The threshold is deliberately semantic rather than a socket timeout: a
    provider can keep the TCP stream healthy for minutes while emitting only
    reasoning.  The 8 KiB default is roughly 2K tokens and is comfortably
    above the 143 reasoning tokens observed before a successful Pi tool call,
    while stopping the 20K-chunk GLM failure long before the one-hour run
    deadline.
    """

    raw = os.getenv("KSADK_REASONING_ONLY_ACTION_BUDGET_CHARS")
    try:
        value = int(raw) if raw is not None else _DEFAULT_REASONING_ONLY_ACTION_BUDGET_CHARS
    except (TypeError, ValueError):
        value = _DEFAULT_REASONING_ONLY_ACTION_BUDGET_CHARS
    return max(1_024, min(value, 65_536))


@dataclass(frozen=True)
class StudioPluginTurnResult:
    """Normalized result retained at the Studio/third-party boundary."""

    output_text: str
    session_id: str
    usage: Mapping[str, Any]
    metadata: Mapping[str, Any]
    raw: Any


@dataclass
class _HostEntry:
    agent_id: str
    bundle: ResolvedPluginBundle
    host: PluginHost




def _parse_tool_arguments(raw: str | None) -> dict[str, Any] | None:
    """解析模型给出的工具参数；非 JSON 对象一律返回 None（不抛错）。

    返回 None 表示"参数形状不可用"：调用方要么修复一次，要么把它作为
    invalid_arguments 回灌给模型，绝不允许升级成整轮致命错误。
    """

    try:
        arguments = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    return arguments if isinstance(arguments, dict) else None


class _StudioHarnessReasoner:
    """Bind the Harness loop to the immutable Studio model/network policy."""

    def __init__(
        self,
        client: OpenAICompatibleModelClient,
        *,
        model: ResolvedModel,
        fallback_models: Sequence[ResolvedModel] = (),
        network_policy: NetworkPolicy,
        timeout_seconds: int,
        max_attempts: int,
        backoff_seconds: float,
        total_timeout_seconds: int | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._models = {
            item.model: item for item in (model, *tuple(fallback_models))
        }
        self._network_policy = network_policy
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        # 整个 run 的预算：增量传输的硬上限（防止"一直吐 keepalive"的流永不结束）。
        self._total_timeout_seconds = max(
            timeout_seconds, int(total_timeout_seconds or timeout_seconds)
        )
        # 传输模式来自"端点声明"（见 ksadk/model_proxy/compat.py），不是代码里的
        # 模型名分支：默认流式；只有真正不支持增量的端点才声明
        # prefer_buffered_transport。曾经的 kspmas 档整条走缓冲，代价是一次
        # 模型调用从发出到返回之间前端没有任何可显示内容（"像卡住"）。
        from ksadk.model_proxy import compat as provider_compat

        self._streaming = not provider_compat.prefer_buffered_transport(
            base_url=self._model.endpoint_url,
            model=self._model.model,
        )
        if hasattr(self._client, "supports_streaming"):
            self._streaming = self._client.supports_streaming(
                self._model, requested=self._streaming
            )
        # "空闲进度"超时：连续这么久没有**内容** chunk 才算卡住（keepalive 不算），
        # 所以慢但持续吐字的回合不会被误杀。
        self._stream_idle_timeout_seconds = max(
            0.001,
            float(
                provider_compat.stream_idle_timeout(
                    base_url=self._model.endpoint_url,
                    model=self._model.model,
                    fallback=timeout_seconds,
                )
                or timeout_seconds
            ),
        )

    def _require_model(self, model: str) -> ResolvedModel:
        try:
            return self._models[model]
        except KeyError as error:
            raise PluginHostError(
                "harness_model_not_bound",
                f"Harness requested unbound model {model!r}",
            ) from error

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
        selected_model = self._require_model(model)
        response = await self._client.complete(
            selected_model,
            messages=[dict(message) for message in messages],
            network_policy=self._network_policy,
            timeout_seconds=self._timeout_seconds,
            max_attempts=self._max_attempts,
            backoff_seconds=self._backoff_seconds,
            tools=[dict(tool.openai_schema) for tool in tools],
            allow_empty=bool(tools),
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            # 触顶一次性升级（对齐 Claude Code 的 max_output_tokens_escalate）：
            # 平时给 slot 友好的小预算，真被截断才扩容重试一次。
            retry_on_length=True,
        )
        calls: list[HarnessToolCall] = []
        invalid_names: list[str] = []

        def _collect(source_calls: Sequence[Any]) -> list[HarnessToolCall]:
            collected: list[HarnessToolCall] = []
            for call in source_calls:
                arguments = _parse_tool_arguments(call.arguments)
                if arguments is None:
                    invalid_names.append(call.name)
                    collected.append(
                        HarnessToolCall(
                            call_id=call.id,
                            name=call.name,
                            arguments={},
                            invalid_reason="model emitted non-JSON-object tool arguments",
                        )
                    )
                    continue
                collected.append(
                    HarnessToolCall(call_id=call.id, name=call.name, arguments=arguments)
                )
            return collected

        calls = _collect(response.tool_calls)
        if any(call.invalid_reason for call in calls):
            # 与流式路径同策略：先给一次修复（更大预算 + 明确要求合法 JSON），
            # 修复仍不可用则保留 invalid_reason，由循环层以 invalid_arguments 的
            # tool result 回灌给模型重试——参数形状问题不中断整轮。
            repair = await self._client.complete(
                selected_model,
                messages=[
                    *[dict(message) for message in messages],
                    {
                        "role": "system",
                        "content": (
                            "The previous tool call could not be parsed. "
                            f"Retry the intended {invalid_names[0]} call once with valid "
                            "JSON arguments. Keep the arguments concise enough to fit in "
                            "one tool call and do not repeat the analysis."
                        ),
                    },
                ],
                network_policy=self._network_policy,
                timeout_seconds=self._timeout_seconds,
                max_attempts=self._max_attempts,
                backoff_seconds=self._backoff_seconds,
                tools=[dict(tool.openai_schema) for tool in tools],
                allow_empty=bool(tools),
                retry_on_length=True,
                max_output_tokens=max(
                    16_384,
                    int(max_output_tokens or selected_model.parameters.max_tokens or 0),
                ),
                reasoning_effort=reasoning_effort,
            )
            repaired = _collect(repair.tool_calls)
            if any(not call.invalid_reason for call in repaired):
                calls = repaired
        reported_usage = response.usage if response.usage.reported else None
        return HarnessReasoningTurn(
            final_text=response.content or None,
            tool_calls=tuple(calls),
            reasoning=response.reasoning or None,
            degraded=(
                "reasoning_only_turn"
                if getattr(response, "empty_reasoning_only", False)
                else None
            ),
            usage=None
            if reported_usage is None
            else {
                "input_tokens": max(0, int(reported_usage.input_tokens)),
                "output_tokens": max(0, int(reported_usage.output_tokens)),
                "cached_tokens": max(0, int(reported_usage.cached_input_tokens)),
                "reasoning_tokens": max(0, int(reported_usage.reasoning_output_tokens)),
            },
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
    ):
        """Pass model deltas through the Harness loop as soon as they arrive.

        传输纪律（本轮修复的"像卡住"根因）：

        - 正文、思考、工具调用或完成信号都是真实传输进展，会顺延空闲
          计时。不能把思考型模型持续返回的 ``reasoning_content`` 当成
          “无进展”；这会在模型正常思考时误杀连接。
        - usage/keepalive 不续命。一条始终只产生思考的流仍受
          ``total_timeout_seconds`` 硬上限约束，不会无限运行。
        - 连续 ``stream_idle_timeout_seconds`` 没有任何内容（只有 keepalive 或
          干脆静默）即判定为卡住：关闭当前流并抛出可重试的超时。外层统一
          provider policy 会立即记录 ``model.call.failed``，再按策略重试或切换
          provider。这里不能偷偷再发一次完整缓冲请求，否则一次 60 秒的流超时
          后还会叠加完整请求超时，Studio 会无进展地假死数分钟。
        - 卡住前已经流出的半截内容会由循环层在失败时清理，不会污染答案或模型
          上下文。
        """
        selected_model = self._require_model(model)
        from ksadk.model_proxy import compat as provider_compat

        stream_idle_timeout_seconds = max(
            0.001,
            float(
                provider_compat.stream_idle_timeout(
                    base_url=selected_model.endpoint_url,
                    model=selected_model.model,
                    fallback=self._timeout_seconds,
                )
                or self._timeout_seconds
            ),
        )
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        reasoning_only_action_chars = 0
        tool_calls: list[HarnessToolCall] = []
        invalid_tool_name: str | None = None
        usage = None
        stream_kwargs = {
            "messages": [dict(message) for message in messages],
            "network_policy": self._network_policy,
            "timeout_seconds": self._timeout_seconds,
            "tools": [dict(tool.openai_schema) for tool in tools],
            "max_output_tokens": max_output_tokens,
            "reasoning_effort": reasoning_effort,
        }
        if hasattr(self._client, "stream_events"):
            # Studio and direct SDK execution both consume the canonical model
            # event vocabulary.  This small compatibility projection preserves
            # the established HarnessReasoner protocol while keeping provider
            # wire chunks out of the loop and UI layers.
            from ksadk.model_gateway.events import ModelStreamEventType
            from ksadk.studio.contracts import Usage
            from ksadk.studio.model_client import ModelStreamChunk, ToolCall

            async def _canonical_chunks():
                async for event in self._client.stream_events(
                    selected_model, **stream_kwargs
                ):
                    if event.type is ModelStreamEventType.TEXT_DELTA:
                        yield ModelStreamChunk(
                            text=event.text,
                            provider_request_id=event.provider_request_id,
                        )
                    elif event.type is ModelStreamEventType.REASONING_DELTA:
                        yield ModelStreamChunk(
                            reasoning=event.text,
                            provider_request_id=event.provider_request_id,
                        )
                    elif event.type is ModelStreamEventType.TOOL_CALL_COMPLETED:
                        yield ModelStreamChunk(
                            tool_calls=(
                                ToolCall(
                                    id=event.call_id,
                                    name=event.tool_name,
                                    arguments=event.arguments,
                                ),
                            ),
                            provider_request_id=event.provider_request_id,
                        )
                    elif event.type is ModelStreamEventType.USAGE_UPDATED:
                        values = event.usage or {}
                        yield ModelStreamChunk(
                            usage=Usage(
                                input_tokens=int(values.get("input_tokens") or 0),
                                output_tokens=int(values.get("output_tokens") or 0),
                                total_tokens=int(values.get("input_tokens") or 0)
                                + int(values.get("output_tokens") or 0),
                                cached_input_tokens=int(values.get("cached_tokens") or 0),
                                reasoning_output_tokens=int(
                                    values.get("reasoning_tokens") or 0
                                ),
                                reported=True,
                                source="model-gateway",
                            ),
                            provider_request_id=event.provider_request_id,
                        )
                    elif event.type is ModelStreamEventType.TURN_COMPLETED:
                        yield ModelStreamChunk(
                            done=True,
                            finish_reason=str(
                                event.metadata.get("finish_reason") or ""
                            ),
                            provider_request_id=event.provider_request_id,
                        )

            stream = _canonical_chunks()
        else:
            stream = self._client.stream(selected_model, **stream_kwargs)
        iterator = stream.__aiter__()
        loop = asyncio.get_running_loop()
        total_deadline = loop.time() + self._total_timeout_seconds
        idle_deadline = loop.time() + stream_idle_timeout_seconds

        async def _raise_idle_timeout(*, close_stream: bool) -> None:
            """终止无进展流，把重试决策交给统一 provider policy。"""

            # ``asyncio.wait_for(iterator.__anext__())`` 超时会先取消并等待当前
            # ``__anext__``，取消会沿着 httpx 流自动释放连接。此时再次并发
            # ``aclose`` 会让 Python 的异步生成器终结器报
            # ``aclose(): asynchronous generator is already running``。只有尚未
            # 启动 ``__anext__`` 就发现预算耗尽时，才需要在这里主动关闭。
            if close_stream:
                try:
                    await stream.aclose()
                except Exception:  # noqa: BLE001 - 关闭失败不能遮蔽真正的空闲超时
                    pass
            raise TimeoutError(
                "model stream made no progress for "
                f"{stream_idle_timeout_seconds:g} seconds"
            )

        while True:
            now = loop.time()
            if now >= total_deadline:
                # 整轮预算耗尽：不再发起新请求，保持原有硬失败语义（由循环层
                # 决定重试或切换候选模型），不做缓冲回退。
                raise TimeoutError("model stream exceeded the run budget")
            budget = min(idle_deadline, total_deadline) - now
            if budget <= 0:
                # 走到这里只可能是空闲进度超时（总预算已在上面拦下）。
                await _raise_idle_timeout(close_stream=True)
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=budget)
            except StopAsyncIteration:
                break
            except (asyncio.TimeoutError, TimeoutError):
                if loop.time() >= total_deadline:
                    raise TimeoutError(
                        "model stream exceeded the run budget"
                    ) from None
                await _raise_idle_timeout(close_stream=False)
            if (chunk.text or chunk.reasoning or chunk.public_summary
                    or chunk.tool_calls or chunk.done):
                # 真实内容增量证明运输活着：正文、思考、工具调用和完成
                # 信号都顺延空闲计时。无限 reasoning-only 回合由 total_deadline
                # 截止；usage/keepalive 仍不续命。
                idle_deadline = loop.time() + stream_idle_timeout_seconds
            if chunk.text:
                text_parts.append(chunk.text)
                yield {"text_delta": chunk.text}
            if chunk.public_summary:
                yield {"summary_delta": chunk.public_summary}
            if chunk.reasoning:
                reasoning_parts.append(chunk.reasoning)
                reasoning_only_action_chars += len(chunk.reasoning)
                yield {"reasoning_delta": chunk.reasoning}
                if (
                    tools
                    and not text_parts
                    and not tool_calls
                    and reasoning_only_action_chars
                    >= _reasoning_only_action_budget_chars()
                ):
                    # The transport is healthy, but the Agent has made no
                    # observable progress.  Live GLM 5.3 Flash comparison
                    # produced 20,366 reasoning chunks and zero tool calls in
                    # Studio; native DeepAgents and bare Pi showed the same
                    # first-response failure.  A measured provider probe
                    # recovered in 1.13s only when thinking was disabled and
                    # one tool call was required.  Apply that narrow recovery
                    # once, then return to the ordinary auto policy on the
                    # following turn.
                    try:
                        await stream.aclose()
                    except Exception:  # noqa: BLE001 - recovery owns the next request
                        pass
                    recovery = await self._client.complete(
                        selected_model,
                        messages=[
                            *[dict(message) for message in messages],
                            {
                                "role": "system",
                                "content": (
                                    "This tool-capable turn spent its action budget on "
                                    "reasoning without making progress. Call exactly one "
                                    "small, relevant tool now to inspect or advance the "
                                    "task. Do not draft the complete implementation or "
                                    "return more reasoning in this recovery turn."
                                ),
                            },
                        ],
                        network_policy=self._network_policy,
                        timeout_seconds=self._timeout_seconds,
                        max_attempts=1,
                        backoff_seconds=0,
                        tools=[dict(tool.openai_schema) for tool in tools],
                        allow_empty=False,
                        retry_on_length=True,
                        max_output_tokens=min(2_048, int(max_output_tokens or 2_048)),
                        reasoning_effort=reasoning_effort,
                        tool_choice="required",
                    )
                    recovered_calls: list[HarnessToolCall] = []
                    for call in recovery.tool_calls:
                        arguments = _parse_tool_arguments(call.arguments)
                        recovered_calls.append(
                            HarnessToolCall(
                                call_id=call.id,
                                name=call.name,
                                arguments=arguments or {},
                                invalid_reason=(
                                    None
                                    if arguments is not None
                                    else "model emitted non-JSON-object tool arguments"
                                ),
                            )
                        )
                    reported_usage = recovery.usage if recovery.usage.reported else None
                    yield {
                        "turn": HarnessReasoningTurn(
                            final_text=recovery.content or None,
                            tool_calls=tuple(recovered_calls),
                            reasoning=recovery.reasoning or None,
                            degraded="reasoning_only_action_recovered",
                            usage=None
                            if reported_usage is None
                            else {
                                "input_tokens": max(
                                    0, int(reported_usage.input_tokens)
                                ),
                                "output_tokens": max(
                                    0, int(reported_usage.output_tokens)
                                ),
                                "cached_tokens": max(
                                    0, int(reported_usage.cached_input_tokens)
                                ),
                                "reasoning_tokens": max(
                                    0, int(reported_usage.reasoning_output_tokens)
                                ),
                            },
                        )
                    }
                    return
            if chunk.usage is not None:
                usage = chunk.usage
            if chunk.tool_calls:
                tool_calls = []
                for call in chunk.tool_calls:
                    try:
                        arguments = json.loads(call.arguments or "{}")
                    except json.JSONDecodeError:
                        invalid_tool_name = call.name
                        continue
                    if not isinstance(arguments, dict):
                        invalid_tool_name = call.name
                        continue
                    tool_calls.append(
                        HarnessToolCall(call_id=call.id, name=call.name, arguments=arguments)
                    )
            if chunk.done:
                if (
                    chunk.finish_reason
                    in {"length", "incomplete", "max_tokens", "max_output_tokens"}
                    and tools
                    and not tool_calls
                ):
                    # Pi's native auto-continuation treats a length stop as an
                    # unfinished turn.  Live KSPMAS evidence showed the same
                    # GLM request drafting prose until the 2K cap with
                    # ``tool_choice=auto``, while a required-action recovery
                    # returned a valid tool call in about one second.  Do not
                    # publish the truncated plan as a final answer.
                    recovery = await self._client.complete(
                        selected_model,
                        messages=[
                            *[dict(message) for message in messages],
                            {
                                "role": "system",
                                "content": (
                                    "The previous tool-capable response was truncated "
                                    "before it took action. Call exactly one small, "
                                    "relevant tool now to advance the task. Do not "
                                    "repeat the plan or draft the complete implementation "
                                    "in this recovery turn."
                                ),
                            },
                        ],
                        network_policy=self._network_policy,
                        timeout_seconds=self._timeout_seconds,
                        max_attempts=1,
                        backoff_seconds=0,
                        tools=[dict(tool.openai_schema) for tool in tools],
                        allow_empty=False,
                        retry_on_length=True,
                        # Unlike reasoning-only recovery, this branch often
                        # carries a whole file in structured arguments.  The
                        # original 2K cap simply truncated the required tool
                        # call a second time.  Match the existing malformed-
                        # argument repair budget so one large write can remain
                        # valid JSON.
                        max_output_tokens=max(
                            16_384,
                            int(
                                max_output_tokens
                                or selected_model.parameters.max_tokens
                                or 0
                            ),
                        ),
                        reasoning_effort=reasoning_effort,
                        tool_choice="required",
                    )
                    recovered_calls: list[HarnessToolCall] = []
                    for call in recovery.tool_calls:
                        arguments = _parse_tool_arguments(call.arguments)
                        recovered_calls.append(
                            HarnessToolCall(
                                call_id=call.id,
                                name=call.name,
                                arguments=arguments or {},
                                invalid_reason=(
                                    None
                                    if arguments is not None
                                    else "model emitted non-JSON-object tool arguments"
                                ),
                            )
                        )
                    reported_usage = recovery.usage if recovery.usage.reported else None
                    yield {
                        "turn": HarnessReasoningTurn(
                            tool_calls=tuple(recovered_calls),
                            reasoning=recovery.reasoning or None,
                            degraded="length_truncated_action_recovered",
                            usage=None
                            if reported_usage is None
                            else {
                                "input_tokens": max(
                                    0, int(reported_usage.input_tokens)
                                ),
                                "output_tokens": max(
                                    0, int(reported_usage.output_tokens)
                                ),
                                "cached_tokens": max(
                                    0, int(reported_usage.cached_input_tokens)
                                ),
                                "reasoning_tokens": max(
                                    0, int(reported_usage.reasoning_output_tokens)
                                ),
                            },
                        )
                    }
                    return
                if invalid_tool_name:
                    # Some OpenAI-compatible GLM gateways truncate or slightly
                    # corrupt a large native function-call JSON object.  The
                    # first attempt's public prose has already streamed, so ask
                    # once for the intended call again with a larger budget and
                    # consume only its structured call.  Raw invalid arguments
                    # are never placed back into model context or Studio events.
                    repair = await self._client.complete(
                        selected_model,
                        messages=[
                            *[dict(message) for message in messages],
                            {
                                "role": "system",
                                "content": (
                                    "The previous tool call could not be parsed. "
                                    f"Retry the intended {invalid_tool_name} call once with "
                                    "valid JSON arguments. Keep the file content concise enough "
                                    "to fit in one tool call and do not repeat the analysis."
                                ),
                            },
                        ],
                        network_policy=self._network_policy,
                        timeout_seconds=self._timeout_seconds,
                        max_attempts=self._max_attempts,
                        backoff_seconds=self._backoff_seconds,
                        tools=[dict(tool.openai_schema) for tool in tools],
                        allow_empty=bool(tools),
                        retry_on_length=True,
                        max_output_tokens=max(
                            16_384,
                            int(
                                max_output_tokens
                                or selected_model.parameters.max_tokens
                                or 0
                            ),
                        ),
                        reasoning_effort=reasoning_effort,
                    )
                    tool_calls = []
                    for call in repair.tool_calls:
                        arguments = _parse_tool_arguments(call.arguments)
                        if arguments is None:
                            # 修复轮仍给出坏参数：保留 invalid_reason，交给循环层
                            # 以 invalid_arguments 回灌，不中断整轮。
                            tool_calls.append(
                                HarnessToolCall(
                                    call_id=call.id,
                                    name=call.name,
                                    arguments={},
                                    invalid_reason=(
                                        "model emitted non-JSON-object tool arguments"
                                    ),
                                )
                            )
                            continue
                        tool_calls.append(
                            HarnessToolCall(
                                call_id=call.id,
                                name=call.name,
                                arguments=arguments,
                            )
                        )
                    # 修复轮完全没有给出调用：按"纯文本回合"收尾（不加人造调用，
                    # 否则会产生没有配对 tool_call 的 tool result）。
                    usage = repair.usage
                yield {
                    "turn": HarnessReasoningTurn(
                        final_text=(
                            "".join(text_parts)
                            or (repair.content if invalid_tool_name else "")
                            or None
                        ),
                        tool_calls=tuple(tool_calls),
                        reasoning=(
                            "".join(reasoning_parts)
                            or (repair.reasoning if invalid_tool_name else "")
                            or None
                        ),
                        usage=None
                        if usage is None
                        else {
                            "input_tokens": max(0, int(usage.input_tokens)),
                            "output_tokens": max(0, int(usage.output_tokens)),
                            "cached_tokens": max(0, int(usage.cached_input_tokens)),
                            "reasoning_tokens": max(0, int(usage.reasoning_output_tokens)),
                        },
                    )
                }


class StudioPluginRuntime(StudioNativeActivationMixin):
    """Resolve, activate, and retain composed providers for Studio sessions."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        build_repository: BuildRepository,
        session_service: BaseSessionService,
        model_client: OpenAICompatibleModelClient,
        secret_resolver: Any,
        harness_reasoner: HarnessReasoner | None = None,
        harness_engine_selector: HarnessEngineSelector | None = None,
        harness_native_adapter_builder: HarnessNativeAdapterBuilder | None = None,
        harness_native_session_enabled: Callable[[str, str], bool] | None = None,
        provider_manifests: Mapping[str, PluginManifest] | None = None,
        provider_factories: Mapping[str, Any] | None = None,
        hook_registrations: tuple[StudioNativeHookRegistration, ...] = (),
        context_registrations: tuple[StudioNativeContextRegistration, ...] = (),
        legacy_harness_sources: Sequence[LegacyHarnessSource] = (),
        dsh_capability_service: Any | None = None,
        resource_dsh_capability_service: Any | None = None,
        resource_authority: Any | None = None,
        resource_connections: Any | None = None,
        resource_actor_ref: str = "local-user",
        native_tenant_id: str = "local-studio",
        native_workspace_id: str | None = None,
        codex_local_launch_resolver: Any = None,
        runtime_executor: RuntimeExecutor | None = None,
    ) -> None:
        self.workspace = workspace
        self.builds = build_repository
        self._session_service = session_service
        self._model_client = model_client
        self._secret_resolver = secret_resolver
        self._harness_reasoner = harness_reasoner
        if (harness_engine_selector is None) != (harness_native_adapter_builder is None):
            raise ValueError("Native Harness requires both host selector and adapter builder")
        self._harness_engine_selector = harness_engine_selector
        self._harness_native_adapter_builder = harness_native_adapter_builder
        self._harness_native_session_enabled = harness_native_session_enabled
        self._provider_manifests = dict(provider_manifests or {})
        self._provider_factories = dict(provider_factories or {})
        self._hook_registrations = tuple(hook_registrations)
        self._context_registrations = tuple(context_registrations)
        self._dsh_capability_service = dsh_capability_service
        self._resource_dsh_capability_service = (
            resource_dsh_capability_service or dsh_capability_service
        )
        self._resource_authority = resource_authority
        self._resource_connections = resource_connections
        self._resource_actor_ref = resource_actor_ref
        self._native_tenant_id = native_tenant_id
        self._native_workspace_id = native_workspace_id
        self._codex_local_launch_resolver = codex_local_launch_resolver
        self._runtime_executor = runtime_executor
        self._legacy_bundles = LegacyBundleAdapter(legacy_harness_sources)
        self._lock = asyncio.Lock()
        self._hosts: dict[str, _HostEntry] = {}
        self._native_activations: dict[tuple[str, str], _NativeActivation] = {}
        self._activation_locks: dict[str, asyncio.Lock] = {}
        self._resource_write_modes: dict[str, str] = {}
        self._admission = RuntimeAdmissionGate()
        self._closed = False
        self.execution_policy_resolver: Any | None = None

    @property
    def _admission_open(self) -> bool:
        return self._admission.is_open

    def replace_provider_registrations(
        self,
        provider_manifests: Mapping[str, PluginManifest],
        provider_factories: Mapping[str, Any],
    ) -> None:
        """Bind one startup snapshot before any provider activation exists."""

        manifests = dict(provider_manifests)
        factories = dict(provider_factories)
        if manifests.keys() != factories.keys():
            raise ValueError(
                "plugin provider manifests and factories must use the same exact references"
            )
        if self._closed:
            raise RuntimeError("cannot replace provider registrations after runtime close")
        if self._hosts:
            raise RuntimeError("cannot replace provider registrations after activation")
        self._provider_manifests = manifests
        self._provider_factories = factories

    @property
    def active_activation_count(self) -> int:
        return sum(entry.host.activation_count for entry in self._hosts.values())

    def resolve(
        self,
        build_id: str,
        *,
        model: str | None = None,
        approval_mode: str | None = None,
    ) -> StudioRunSpec:
        build = self.builds.get(build_id)
        runtime_type = build.runtime_type.strip().lower()
        if runtime_type not in _COMPOSED_RUNTIME_TYPES:
            raise StudioError(
                "BUILD_RUNTIME_UNSUPPORTED",
                "Build 不是 PluginHost Harness/Provider Runtime",
                status_code=422,
                details={"buildId": build_id, "runtimeType": runtime_type},
            )
        bundle_root = self._bundle_root(build)
        bundle = self._resolve_bundle(bundle_root)
        self._preflight_bundle(bundle)
        dynamic_dsh_mcp = self._bundle_uses_dynamic_dsh(bundle)
        selected_model = self._select_model(build, model)
        resolved = bundle.resolved_agent_spec
        primary_model = resolved.get("model")
        candidate_models = [primary_model]
        fallback_models = resolved.get("fallbackModels") or resolved.get("fallback_models")
        if isinstance(fallback_models, list):
            candidate_models.extend(fallback_models)
        selected_model_spec = next(
            (
                item
                for item in candidate_models
                if isinstance(item, Mapping) and str(item.get("model") or "") == selected_model
            ),
            primary_model if isinstance(primary_model, Mapping) else {},
        )
        instructions = resolved.get("instructions")
        instructions = instructions if isinstance(instructions, Mapping) else {}
        context_spec = resolved.get("context")
        context_spec = context_spec if isinstance(context_spec, Mapping) else {}
        max_input_tokens = context_spec.get("maxInputTokens") or context_spec.get(
            "max_input_tokens"
        )
        reserve_output_tokens = context_spec.get(
            "reserveOutputTokens"
        ) or context_spec.get("reserve_output_tokens")
        provider_id, _provider_version = _parse_plugin_ref(
            bundle.composition.profile.agent_provider.ref
        )
        provider_runtime_type = {
            CODEX_AGENT_PROVIDER_PLUGIN_ID: "codex",
            KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID: "harness",
        }.get(provider_id)
        execution_spec = resolved.get("execution")
        execution_spec = execution_spec if isinstance(execution_spec, Mapping) else {}
        approval_timeout = execution_spec.get("approvalTimeoutSeconds")
        return StudioRunSpec(
            launch_context=RuntimeLaunchContext(
                runtime_type=runtime_type,
                project_dir=bundle_root,
                config={"plugin_bundle_digest": bundle.bundle_digest},
            ),
            build_id=build.id,
            agent_id=build.agent_id,
            model=selected_model,
            model_metadata=dict(selected_model_spec.get("metadata") or {}),
            request_config={
                "agent_system": str(instructions.get("system") or ""),
                "agent_task": str(instructions.get("task") or ""),
                "plugin_bundle_digest": bundle.bundle_digest,
                "dynamic_dsh_mcp": dynamic_dsh_mcp,
                "provider_runtime_adapter": provider_runtime_type is not None,
                "provider_runtime_type": provider_runtime_type or "",
                "provider_ref": bundle.composition.profile.agent_provider.ref,
                **(
                    {"approval_timeout_seconds": int(approval_timeout)}
                    if provider_runtime_type == "harness" and approval_timeout is not None
                    else {}
                ),
                # Provider-backed Harness runs use this resolver rather than
                # FrameworkRunSpecResolver. Preserve the immutable AgentVersion
                # budget here as well, otherwise ManagedHarnessRuntimeAdapter
                # cannot derive the real model window and silently falls back
                # to 32K for every Studio run.
                **(
                    {"max_input_tokens": int(max_input_tokens)}
                    if max_input_tokens is not None
                    else {}
                ),
                **(
                    {"reserve_output_tokens": int(reserve_output_tokens)}
                    if reserve_output_tokens is not None
                    else {}
                ),
                **({"tool_approval_mode": normalize_tool_approval_mode(approval_mode)}
                   if provider_runtime_type == "harness" or approval_mode else {}),
            },
            manifest_sha256=build.resolved_digest,
            plugin_bundle_root=bundle_root,
        )

    async def execute(
        self,
        spec: StudioRunSpec,
        request: Mapping[str, Any],
        *,
        session_id: str,
    ) -> StudioPluginTurnResult:
        if spec.plugin_bundle_root is None:
            raise PluginHostError(
                "plugin_bundle_unavailable", "Studio run has no PluginHost Bundle"
            )
        entry = await self._admission.resolve(lambda: self._host_for(spec.plugin_bundle_root))
        if entry.agent_id != spec.agent_id:
            raise PluginHostError(
                "plugin_bundle_agent_mismatch",
                "Studio run Agent does not match its immutable PluginHost Bundle",
            )
        self._resource_write_modes[session_id] = str(
            spec.request_config.get("tool_approval_mode") or ""
        )
        try:
            activation = await entry.host.open_activation(
                entry.bundle,
                activation_key=session_id,
                activation_claims=await self._native_activation_claims(spec, session_id),
            )
        except BaseException:
            self._resource_write_modes.pop(session_id, None)
            raise
        try:
            raw = await activation.execute(dict(request))
            return _normalize_result(raw, session_id=session_id)
        finally:
            # Dynamic DSH leases are generation-bound. Rebuilding the
            # activation per turn guarantees a restarted sidecar never leaves
            # a session pinned to an expired port or scoped token.
            if self._bundle_uses_dynamic_dsh(entry.bundle):
                try:
                    await activation.close()
                finally:
                    self._resource_write_modes.pop(session_id, None)

    def kernel_adapter_provider(self, spec: StudioRunSpec):  # type: ignore[no-untyped-def]
        """Return a lazy, Build-pinned adapter factory for Scheduler Kernel."""

        if spec.plugin_bundle_root is None:
            raise StudioError(
                "PLUGIN_RUNTIME_UNAVAILABLE",
                "Studio Build 没有 PluginHost Bundle",
                status_code=409,
            )
        return lambda: StudioPluginKernelAdapter(self, spec)






    async def close_session_if_dynamic(self, spec: StudioRunSpec, session_id: str) -> None:
        # 每轮运行后关闭 dynamic-DSH MCP 激活会让下一轮重新 spawn/health/list
        # 全部 MCP server（实测 ~12s），这是 harness 首 token 延迟的主因。
        # open_activation 以 session 为 key 复用同一激活（图与 Bundle digest
        # 不变），因此跨轮保留激活；激活只在会话删除（close_session）与
        # Studio 退出（aclose）时释放。
        return None

    def check_configuration_mutable(self) -> None:
        if self._hosts:
            raise StudioError(
                "SETTINGS_RUNTIME_BUSY", "请通过设置接口刷新运行配置", status_code=409
            )

    def replace_resource_authority(self, authority: Any) -> None:
        self.check_configuration_mutable()
        self._resource_authority = authority

    async def suspend_admission(self) -> None:
        """Stop new activations and drain every currently owned host."""

        async with self._lock:
            self._admission.suspend()
            entries = tuple(self._hosts.values())
            self._hosts.clear()
        cleanup = asyncio.create_task(self._dispose_entries(entries))
        interrupted = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                interrupted = True
        cleanup.result()
        if interrupted:
            raise asyncio.CancelledError

    async def resume_admission(self) -> None:
        async with self._lock:
            if self._closed:
                raise PluginHostError(
                    "plugin_runtime_closed",
                    "Studio plugin runtime is closed",
                )
            self._admission.resume()

    @staticmethod
    async def _dispose_entries(entries: Sequence[_HostEntry]) -> None:
        if entries:
            results = await asyncio.gather(
                *(entry.host.dispose() for entry in entries),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result

    async def aclose(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            self._admission.close()
            entries = tuple(self._hosts.values())
            self._hosts.clear()
        cleanup = asyncio.create_task(self._dispose_entries(entries))
        interrupted = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                interrupted = True
        cleanup.result()
        if interrupted:
            raise asyncio.CancelledError

    async def _host_for(self, bundle_root: Path) -> _HostEntry:
        # Re-resolve every turn. This deliberately rechecks enabled receipts and
        # package digests rather than trusting a previously healthy child.
        bundle = self._resolve_bundle(bundle_root)
        if any(
            item.ref == PLATFORM_RESOURCE_MCP_REF
            for item in bundle.composition.profile.capabilities
        ) and not callable(getattr(self._resource_authority, "admit_runtime", None)):
            raise StudioError(
                "PLATFORM_RESOURCE_CONFIGURATION_REQUIRED",
                "平台资源连接尚未配置，请在设置的云端连接中填写账号凭据和控制面地址",
                status_code=422,
            )
        key = bundle.bundle_digest
        await self._admission.wait()
        async with self._lock:
            if self._closed:
                raise PluginHostError(
                    "plugin_runtime_closed",
                    "Studio plugin runtime is closed",
                )
            if not self._admission.is_open:
                raise PluginHostError(
                    "plugin_runtime_reconfiguring",
                    "Studio plugin runtime is reconfiguring",
                )
            existing = self._hosts.get(key)
            if existing is not None:
                return existing

            registry, factories, permissions = self._runtime_components(bundle)
            verified = (
                bundle
                if bundle.manifest.bundle_format == "agentkit.bundle/v1"
                else PluginBundleResolver(registry).resolve(bundle_root)
            )
            services: dict[str, Any] = {
                "session_service": self._session_service,
                "runtime_state_root": self.workspace.resolve(".agentkit/plugin-runtime/state"),
                # Providers resolve credential *references* at activation time.
                # The DSH discovery host never receives this service.
                "credential_resolver": self._secret_resolver,
                # Harness Provider 持久 Checkpoint/RunStore/Receipt 的状态根
                # （与 builtin capability factories 同一 Workspace 命名空间）。
                "harness_state_dir": str(self.workspace.resolve(".agentkit/plugin-runtime/state")),
                # 用户当前选择的 Studio 工作区：harness 的文件工具应写在这里，
                # 而不是 plugin-runtime state 的哈希目录里。
                "harness_workspace_root": str(self.workspace.root),
                "codex_local_launch_resolver": self._codex_local_launch_resolver,
                "runtime_executor": self._runtime_executor,
                "execution_policy_resolver": self.execution_policy_resolver,
                "dsh_capability_service": self._resource_dsh_capability_service,
                "resource_authority": self._resource_authority,
                "resource_connections": self._resource_connections,
                "resource_actor_ref": self._resource_actor_ref,
                "resource_write_authorizer_factory": self._resource_write_authorizer,
            }
            provider_id, _provider_version = _parse_plugin_ref(
                verified.composition.profile.agent_provider.ref
            )
            if provider_id == KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID:
                services["harness_reasoner"] = (
                    self._harness_reasoner or self._bound_harness_reasoner(verified)
                )
                if self._harness_engine_selector is not None:
                    services["harness_engine_selector"] = self._harness_engine_selector
                    services["harness_native_adapter_builder"] = (
                        self._harness_native_adapter_builder
                    )
            host = PluginHost(
                registry,
                factories,
                allowed_permissions=permissions,
                services=services,
            )
            try:
                await host.apply(verified.composition.profile)
            except BaseException:
                await host.dispose()
                raise
            candidate = _HostEntry(
                agent_id=verified.manifest.agent_id,
                bundle=verified,
                host=host,
            )
            self._hosts[key] = candidate
            # Distinct immutable Builds of the same Agent may be active in
            # different authorized sessions. Their profile graphs are owned
            # until explicit maintenance/close, never evicted by another Build.
            return candidate

    def _resource_write_authorizer(
        self,
        activation_key: str,
        _bundle: ResolvedPluginBundle,
    ) -> FullAccessResourceWriteAuthorizer | None:
        if self._resource_write_modes.get(activation_key) != "full":
            return None
        return FullAccessResourceWriteAuthorizer(activation_key=activation_key)

    @staticmethod
    def _bundle_uses_dynamic_dsh(bundle: ResolvedPluginBundle) -> bool:
        reference = (
            f"plugin://{DSH_PROFILE_MCP_PLUGIN_ID}@{dsh_profile_mcp_manifest().metadata.version}"
        )
        dynamic_refs = {reference, PLATFORM_RESOURCE_MCP_REF}
        return any(
            capability.ref in dynamic_refs for capability in bundle.composition.profile.capabilities
        )

    def _resolve_bundle(self, bundle_root: Path) -> ResolvedPluginBundle:
        registered_ids = {manifest.metadata.id for manifest in self._provider_manifests.values()}
        try:
            manifest, selection = self._legacy_bundles.select_from_bundle(
                bundle_root,
                registered_provider_ids=registered_ids,
            )
        except LegacyBundleCompatibilityError as error:
            code = (
                "AGENT_PROVIDER_NOT_REGISTERED"
                if error.code == "agent_provider_not_registered"
                else "PLUGIN_BUNDLE_INVALID"
            )
            raise StudioError(
                code,
                "Harness Bundle 兼容性校验失败",
                status_code=409,
                details={"reason": error.code},
            ) from error
        if selection is not None and selection.route == "legacy":
            assert selection.manifest is not None
            profile = CompositionProfile.model_validate(
                {
                    "agentProvider": {
                        "ref": (
                            f"plugin://{selection.manifest.metadata.id}"
                            f"@{selection.manifest.metadata.version}"
                        )
                    }
                }
            )
            registry = PluginRegistry([selection.manifest, *builtin_capability_manifests()])
            try:
                resolved = json.loads(
                    (bundle_root / "resolved-agent-spec.json").read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise StudioError(
                    "PLUGIN_BUNDLE_INVALID",
                    "Legacy Harness Bundle 缺少 resolved Agent spec",
                    status_code=409,
                    details={"reason": "legacy_resolved_spec_invalid"},
                ) from error
            if not isinstance(resolved, dict):
                raise StudioError(
                    "PLUGIN_BUNDLE_INVALID",
                    "Legacy Harness resolved Agent spec 必须是对象",
                    status_code=409,
                    details={"reason": "legacy_resolved_spec_invalid"},
                )
            return ResolvedPluginBundle(
                root=bundle_root,
                manifest=manifest,
                resolved_agent_spec=resolved,
                composition=registry.resolve(profile),
            )
        profile = self._read_profile(bundle_root)
        manifests = [
            *builtin_agent_provider_manifests(),
            *builtin_capability_manifests(),
            dsh_profile_mcp_manifest(),
            platform_resource_mcp_manifest(),
            *(item.manifest for item in self._hook_registrations
              if manifest.agent_id in item.agent_ids),
            *(item.manifest for item in self._context_registrations
              if manifest.agent_id in item.agent_ids),
        ]
        external = self._external_manifest(profile)
        if external is not None:
            manifests.append(external)
        try:
            return PluginBundleResolver(PluginRegistry(manifests)).resolve(bundle_root)
        except PluginBundleError as error:
            raise StudioError(
                "PLUGIN_BUNDLE_INVALID",
                "PluginHost Bundle 校验失败",
                status_code=409,
                details={"reason": error.code},
            ) from error

    def _runtime_components(
        self,
        bundle: ResolvedPluginBundle,
    ) -> tuple[PluginRegistry, dict[str, Any], frozenset[str]]:
        manifests: list[PluginManifest] = [
            *builtin_agent_provider_manifests(),
            *builtin_capability_manifests(),
            dsh_profile_mcp_manifest(),
            platform_resource_mcp_manifest(),
        ]
        factories = builtin_capability_factories(
            state_root=self.workspace.resolve(".agentkit/plugin-runtime/state"),
            secret_resolver=self._secret_resolver.resolve,
        )
        factories[DSH_PROFILE_MCP_PLUGIN_ID] = DshProfileMCPFactory(self._dsh_capability_service)
        factories[PLATFORM_RESOURCE_MCP_PLUGIN_ID] = PlatformResourceMCPFactory()
        for item in (*self._hook_registrations, *self._context_registrations):
            if bundle.manifest.agent_id in item.agent_ids:
                manifests.append(item.manifest)
                factories[item.manifest.metadata.id] = item.factory
        provider_id, provider_version = _parse_plugin_ref(
            bundle.composition.profile.agent_provider.ref
        )
        provider_ref = f"plugin://{provider_id}@{provider_version}"
        if bundle.manifest.bundle_format == "agentkit.bundle/v1":
            manifest = legacy_harness_agent_provider_manifest()
            factory = KsADKHarnessProviderFactory(session_service=self._session_service)
        else:
            manifest = self._provider_manifests.get(provider_ref)
            factory = self._provider_factories.get(provider_ref)
        if manifest is None or factory is None:
            raise StudioError(
                "AGENT_PROVIDER_NOT_REGISTERED",
                "AgentProvider 尚未由当前 DSH Profile 完成预检与注册",
                status_code=409,
                details={"provider": provider_ref},
            )
        manifests.append(manifest)
        factories[provider_id] = factory

        allowed = set(builtin_capability_permissions(bundle.composition.profile))
        builtin_ids = {
            manifest.metadata.id
            for manifest in (
                *builtin_agent_provider_manifests(),
                *builtin_capability_manifests(),
                dsh_profile_mcp_manifest(),
                platform_resource_mcp_manifest(),
            )
        }
        builtin_ids.discard(DSH_PROFILE_MCP_PLUGIN_ID)
        allowed.update(
            permission
            for manifest in manifests
            if manifest.metadata.id in builtin_ids
            for permission in manifest.spec.permissions
        )
        security = bundle.resolved_agent_spec.get("security")
        if isinstance(security, Mapping):
            raw = security.get("allowedPermissions") or security.get("allowed_permissions") or []
            if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
                allowed.update(str(item) for item in raw)
        return PluginRegistry(manifests), factories, frozenset(allowed)

    def _preflight_bundle(self, bundle: ResolvedPluginBundle) -> None:
        """Reject an unusable composition before a Run or Schedule is admitted.

        Provider activation is intentionally lazy, but permission and graph
        admission are deterministic from the immutable Bundle.  Delaying this
        check until ``RuntimeAdapter.start`` lets an accepted Inbox message
        repeatedly create PENDING Runs that can never become runnable.
        """

        registry, factories, permissions = self._runtime_components(bundle)
        host = PluginHost(
            registry,
            factories,
            allowed_permissions=permissions,
        )
        try:
            host.preflight(bundle.composition.profile)
        except PluginHostError as error:
            if error.code == "plugin_permission_denied":
                raise StudioError(
                    "PLUGIN_PERMISSION_DENIED",
                    "Agent 插件组合未通过运行前检查",
                    status_code=409,
                    details=_permission_denial_details(error),
                ) from error
            code = (
                "PLUGIN_PERMISSION_DENIED"
                if error.code == "plugin_permission_denied"
                else "PLUGIN_RUNTIME_PREFLIGHT_FAILED"
            )
            raise StudioError(
                code,
                "Agent 插件组合未通过运行前检查",
                status_code=409,
                details={"reason": error.code},
            ) from error

    def _bound_harness_reasoner(self, bundle: ResolvedPluginBundle) -> HarnessReasoner:
        spec = bundle.resolved_agent_spec
        model = ResolvedModel.model_validate(spec.get("model"))
        raw_fallback_models = spec.get("fallbackModels") or spec.get("fallback_models") or []
        fallback_models = tuple(
            ResolvedModel.model_validate(item)
            for item in raw_fallback_models
            if isinstance(item, Mapping)
        )
        security = spec.get("security")
        security = security if isinstance(security, Mapping) else {}
        network = NetworkPolicy.model_validate(security.get("network") or {})
        execution = spec.get("execution")
        execution = execution if isinstance(execution, Mapping) else {}
        # Managed Harness owns provider retry/failover in ModelProviderPolicy.
        # Retrying again inside the HTTP client multiplies attempts (for
        # example 3 outer attempts x 2 hidden HTTP attempts), makes the public
        # attempt counter dishonest, and applies two independent backoffs.
        # Keep this transport call single-shot; the Harness loop performs the
        # visible ten-attempt policy and emits one durable event per retry.
        return _StudioHarnessReasoner(
            self._model_client,
            model=model,
            fallback_models=fallback_models,
            network_policy=network,
            timeout_seconds=_model_request_timeout_seconds(execution),
            # 增量传输的硬上限用整个 run 的预算；上面的 timeout 只用于"无进度"。
            total_timeout_seconds=_model_total_timeout_seconds(execution),
            max_attempts=1,
            backoff_seconds=0,
        )

    def _external_manifest(self, profile: CompositionProfile) -> PluginManifest | None:
        plugin_id, version = _parse_plugin_ref(profile.agent_provider.ref)
        builtin_ids = {manifest.metadata.id for manifest in builtin_agent_provider_manifests()}
        if plugin_id in builtin_ids:
            return None
        provider_ref = f"plugin://{plugin_id}@{version}"
        manifest = self._provider_manifests.get(provider_ref)
        if manifest is None:
            raise StudioError(
                "AGENT_PROVIDER_NOT_REGISTERED",
                "AgentProvider 尚未由当前 DSH Profile 完成预检与注册",
                status_code=409,
                details={"provider": provider_ref},
            )
        return manifest

    def _read_profile(self, bundle_root: Path) -> CompositionProfile:
        try:
            return cast(
                CompositionProfile,
                CompositionProfile.model_validate_json(
                    (bundle_root / "composition-profile.json").read_text(encoding="utf-8")
                ),
            )
        except (OSError, UnicodeError, ValueError) as error:
            raise StudioError(
                "PLUGIN_BUNDLE_INVALID",
                "PluginHost Bundle 缺少有效的 Composition Profile",
                status_code=409,
            ) from error

    def _bundle_root(self, build: BuildRecord) -> Path:
        if not build.artifact_path:
            raise StudioError("BUILD_NOT_READY", "Build 尚未生成制品", status_code=409)
        archive = cast(
            Path,
            self.workspace.resolve(build.artifact_path, must_exist=True),
        )
        bundle_root = archive.parent / "agent-bundle"
        if not bundle_root.is_dir():
            raise StudioError(
                "PLUGIN_BUNDLE_UNAVAILABLE",
                "Build 缺少可运行的 PluginHost Bundle",
                status_code=409,
            )
        return bundle_root

    @staticmethod
    def _select_model(build: BuildRecord, requested: str | None) -> str:
        allowed = [str(item) for item in build.runtime_lock.get("models") or [] if str(item)]
        default = str(build.runtime_lock.get("model") or "").strip()
        if default and default not in allowed:
            allowed.insert(0, default)
        selected = str(requested or default).strip()
        if not selected:
            raise StudioError("AGENT_MODEL_REQUIRED", "Build 没有绑定可运行模型", status_code=422)
        if allowed and selected not in allowed:
            raise StudioError(
                "MODEL_NOT_BOUND",
                "请求模型未绑定到当前 Agent Build",
                status_code=422,
                details={"model": selected, "allowedModels": allowed},
            )
        return selected


def _parse_plugin_ref(value: str) -> tuple[str, str]:
    plugin_id, version = value.removeprefix("plugin://").rsplit("@", 1)
    return plugin_id, version


_PERMISSION_DENIAL_MARKER = "requests unapproved permissions:"


def _permission_denial_details(error: PluginHostError) -> dict[str, Any]:
    """把权限拒绝错误转成可自助修复的 details（缺失权限 + 修复指引）。"""

    message = str(error)
    missing: list[str] = []
    if _PERMISSION_DENIAL_MARKER in message:
        tail = message.split(_PERMISSION_DENIAL_MARKER, 1)[1]
        missing = [item.strip() for item in tail.split(",") if item.strip()]
    return {
        "reason": error.code,
        "missingPermissions": missing,
        "hint": (
            "在 Agent 安全设置的允许权限中加入缺失权限后重新构建并运行"
            if missing
            else "在 Agent 安全设置中调整允许权限后重新构建并运行"
        ),
    }


def _normalize_result(raw: Any, *, session_id: str) -> StudioPluginTurnResult:
    if isinstance(raw, (CodexTurnResult, HarnessTurnResult)):
        return StudioPluginTurnResult(
            output_text=raw.output_text,
            session_id=raw.session_id,
            usage=dict(raw.usage),
            metadata=dict(raw.metadata),
            raw=raw,
        )
    if not isinstance(raw, Mapping):
        raise PluginHostError("provider_result_invalid", "AgentProvider result must be an object")
    output_text = str(raw.get("outputText") or raw.get("output_text") or raw.get("output") or "")
    if not output_text:
        raise PluginHostError(
            "provider_result_invalid", "AgentProvider result must contain outputText"
        )
    usage = raw.get("usage") if isinstance(raw.get("usage"), Mapping) else {}
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}
    return StudioPluginTurnResult(
        output_text=output_text,
        session_id=str(raw.get("sessionId") or raw.get("session_id") or session_id),
        usage=dict(usage),
        metadata=dict(metadata),
        raw=raw,
    )


__all__ = ["StudioPluginRuntime", "StudioPluginTurnResult"]
