# ksadk/harness/engine/hooked_reasoner.py
"""G5 §5.2：on_provider_request / on_provider_response 的 Reasoner 包装器。

纪律：
- 引擎仅在 hooks 非空（或子 Agent 继承已包装 Reasoner）时包装 ``self._reasoner``；
  无钩子时不包装，行为与现状逐字节相同。
- ``complete`` 入口跑 ``on_provider_request``：钩子可改写 ``messages``（脱敏/
  裁剪），但 ``model`` 键的改写一律被强制回写为原值（spec §5.2：钩子无权限改
  模型路由）。拿到 turn 后跑 ``on_provider_response`` 只读观测（{"model",
  "usage"}）。
- ``stream_complete`` 同样在入口跑 request 钩子；捕获最终 ``{"turn": ...}``
  chunk 后先跑 response 钩子再原样 yield（保持 chunk 形态不变）。
- 钩子执行上下文取「引擎活跃 Run」（``engine._active_hook_run``，由
  graph_builder 在模型调用节点设置、run 收尾清空）。无活跃 Run（如脱离图
  执行的探测调用）时跳过钩子、丢弃事件并 stderr 记录——不在任何 Run 事件
  流里虚构归属。
- 钩子异常/协议违约一律可见降级：降级事件先入引擎级缓冲
  （``engine._pending_hook_degradations``，带 Run 归属），由 reason 节点在
  合并完 loop 事件（``run.seq`` 已被正确推进）之后统一 flush。**不得**在
  turn 进行中直接调 ``engine._event``——那会让 ``run.seq`` 领先于 loop 以
  ``seq_start`` 计的本地编号，导致 turn 事件被丢弃（非流式丢
  model.call.started）或 seq 撞号（流式 live sink）。

已知限制（G5 §5.2 provider 钩子的执行边界）：
- 压缩/规划期模型调用「不经」request 钩子：``ManagedLangGraphEngine._execute``
  在建图之前先跑 ``prepare_context``（其摘要调用经 ``context_pipeline`` 发起），
  此时尚无活跃 Run 上下文（``_before_model`` 尚未执行），钩子按「无归属」跳过
  并 stderr 记录。
- ``recover_model_overflow`` 路径「经」钩子：它发生在 reason 节点内部
  （``_before_model`` 之后），活跃 Run 上下文已就位，因此 request/response
  钩子照常执行。
"""

from __future__ import annotations

import sys
from typing import Any, AsyncIterator, Sequence

from ksadk.harness.events import EventType
from ksadk.harness.hooks import RunContext, run_hooks

#: 引擎内部留痕键：审计侧（状态/tool.call.end）保留，Provider 请求面必须剥离。
#: 这些键不是 OpenAI Chat 消息契约的一部分，严格 Provider 会按未知字段拒绝。
_INTERNAL_MESSAGE_KEYS = frozenset({"modified_by_hook"})


def _strip_internal_message_keys(
    messages: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """剥离进入 Provider 的引擎内部留痕键（非 dict 元素原样透传）。

    ``modified_by_hook`` 等键是引擎/审计面的留痕（tool.call.end、图状态），
    不是 Provider 消息契约字段；原样发出会被严格 Provider 以未知字段拒绝
    （400/422）。此处只做删键，不改动任何其他键或值——无该键的消息逐字节
    透传。
    """
    return [
        {key: value for key, value in message.items() if key not in _INTERNAL_MESSAGE_KEYS}
        if isinstance(message, dict)
        else message
        for message in messages
    ]


class _HookedReasoner:
    """可降级包装的 HarnessReasoner：实现 complete/stream_complete 协议。"""

    def __init__(self, inner: Any, hooks: tuple[Any, ...], engine: Any) -> None:
        self._inner = inner
        self._hooks = tuple(hooks)
        self._engine = engine

    # -------------------------------------------------------------- 属性透传

    @property
    def _streaming(self) -> Any:
        """graph_builder 以 getattr(reasoner, "_streaming", None) 探测流式能力。"""
        return getattr(self._inner, "_streaming", None)

    @property
    def hooks(self) -> tuple[Any, ...]:
        """子 Agent 引擎重绑定时沿用同一钩子链（只读）。"""
        return self._hooks

    def __getattr__(self, name: str) -> Any:
        # 其余 Reasoner 能力（如未来的 Provider 探测方法）一律透传 inner。
        # __getattr__ 仅在实例属性缺失时调用；_inner 已在 __init__ 落盘。
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    def rebind(self, engine: Any) -> "_HookedReasoner":
        """子 Agent 引擎复用父引擎包装器时重绑定：钩子链不变，事件归属子 Run。"""
        return type(self)(self._inner, self._hooks, engine)

    # ---------------------------------------------------------------- 调用面

    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[Any],
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> Any:
        run_ctx = self._hook_run_context()
        hooked_messages = await self._apply_request_hooks(run_ctx, model, messages)
        turn = await self._inner.complete(
            model=model,
            prompt=prompt,
            messages=hooked_messages,
            tools=tools,
            **self._request_options(max_output_tokens, reasoning_effort),
        )
        await self._response_observed(run_ctx, model, turn)
        return turn

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
        run_ctx = self._hook_run_context()
        hooked_messages = await self._apply_request_hooks(run_ctx, model, messages)
        async for item in self._inner.stream_complete(
            model=model,
            prompt=prompt,
            messages=hooked_messages,
            tools=tools,
            **self._request_options(max_output_tokens, reasoning_effort),
        ):
            if "turn" in item:
                # 最终 turn chunk：先跑 response 钩子再透传（观测点在产出之后）。
                await self._response_observed(run_ctx, model, item["turn"])
            yield item

    # ---------------------------------------------------------------- 内部

    @staticmethod
    def _request_options(
        max_output_tokens: int | None,
        reasoning_effort: str | None,
    ) -> dict[str, Any]:
        # 与 loop/reason.py 的条件 kwarg 语义一致：None 不传出，保持 inner 默认值。
        return {
            **(
                {"max_output_tokens": max_output_tokens}
                if max_output_tokens is not None
                else {}
            ),
            **(
                {"reasoning_effort": reasoning_effort}
                if reasoning_effort
                else {}
            ),
        }

    def _hook_run_context(self) -> RunContext | None:
        """从引擎活跃 Run 取钩子上下文；无活跃 Run 时跳过钩子并 stderr 记录。"""
        run = getattr(self._engine, "_active_hook_run", None)
        if run is None:
            print(
                "[ksadk.harness.hooks] provider hook event dropped: no active hook run context",
                file=sys.stderr,
            )
            return None
        return RunContext(
            run_id=str(run.handle.run_id),
            agent_id=str(run.state.agent_id),
            user_id=str(run.state.user_id or ""),
            session_id=str(run.state.session_id or ""),
        )

    def _hook_event(self, run_ctx: RunContext, event_type: str, payload: dict) -> None:
        """run_hooks 的降级上报通道：写入引擎级缓冲，由模型调用节点统一 flush。

        降级事实当轮不能直接 ``engine._event``：那会让 ``run.seq`` 领先于 loop
        以 ``seq_start`` 计的本地编号，导致 turn 事件被丢弃或 seq 撞号。缓冲项
        带 Run 归属，flush 按 Run 取（seq 单调由 ``engine._event`` 保证）。

        活跃 Run 与 run_ctx 不再对应（如 Run 已收尾被清空）时丢弃 + stderr，
        不虚构事件归属。
        """
        run = getattr(self._engine, "_active_hook_run", None)
        if run is None or str(run.handle.run_id) != run_ctx.run_id:
            print(
                "[ksadk.harness.hooks] provider hook event dropped: "
                f"active run changed ({run_ctx.run_id!r})",
                file=sys.stderr,
            )
            return
        buffer_hook_degradation(self._engine, run_ctx.run_id, event_type, payload)

    async def _apply_request_hooks(
        self,
        run_ctx: RunContext | None,
        model: str,
        messages: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """入口跑 on_provider_request；返回送入 inner 的 messages。

        model 键强制回写原值；钩子返回非 dict 或 messages 非 list 属协议违约，
        可见降级（policy.decision）并用原始 messages 继续。消息进入 inner 前
        一律剥离引擎内部留痕键（``modified_by_hook`` 等）——它们只存在于审计面
        （tool.call.end / 图状态），不是 Provider Chat 消息契约字段。
        """
        original = [dict(m) for m in messages]
        if run_ctx is None:
            return _strip_internal_message_keys(original)
        hooked = await run_hooks(
            self._hooks,
            "on_provider_request",
            run_ctx,
            self._hook_event,
            payload={"model": model, "messages": [dict(m) for m in original]},
        )
        if not isinstance(hooked, dict):
            # run_hooks 链式守卫下不可达；纵深防御。
            self._degrade(run_ctx, "non-dict", "on_provider_request")
            return _strip_internal_message_keys(original)
        # model 键强制回写原值：钩子可改写 messages，不得改模型路由。
        hooked["model"] = model
        hooked_messages = hooked.get("messages", original)
        if not isinstance(hooked_messages, list):
            # run_hooks 链式守卫下不可达；纵深防御。
            self._degrade(run_ctx, "non-list", "on_provider_request")
            return _strip_internal_message_keys(original)
        return _strip_internal_message_keys(hooked_messages)

    def _degrade(self, run_ctx: RunContext, code: str, method: str) -> None:
        """协议违约的可见降级：与 run_hooks 相同的 policy.decision 事件形态。

        G26：协议违约一律 non_blocking_error（不阻断），hook_type 默认 prompt
        （provider 钩子的违约无声明类型信息，取默认）。
        """
        self._hook_event(
            run_ctx,
            EventType.POLICY_DECISION,
            {
                "action": "hook_failed",
                "reason": f"{method}: protocol violation ({code}); original payload used",
                "outcome": "hook_failed",
                "hook_outcome": "non_blocking_error",
                "hook_type": "prompt",
                "method": method,
                "error": code,
            },
        )

    async def _response_observed(self, run_ctx: RunContext | None, model: str, turn: Any) -> None:
        if run_ctx is None:
            return
        usage = getattr(turn, "usage", None) or {}
        await run_hooks(
            self._hooks,
            "on_provider_response",
            run_ctx,
            self._hook_event,
            payload={"model": model, "usage": dict(usage)},
        )


def buffer_hook_degradation(
    engine: Any, run_id: str, event_type: str, payload: dict[str, Any]
) -> None:
    """钩子降级的统一入缓冲入口（模型调用节点与 tool_calls 节点共用）。

    降级事实当轮不得直接 ``engine._event``：那会让 ``run.seq`` 领先于 loop 以
    ``seq_start`` 计的本地编号（详见模块 docstring）。缓冲项带 Run 归属，
    由各节点在合并完本节点事件后调用 :func:`flush_hook_degradations` 统一落到
    本 Run 的事件流。
    """
    buffer = getattr(engine, "_pending_hook_degradations", None)
    if buffer is None:
        buffer = []
        engine._pending_hook_degradations = buffer
    buffer.append({"run_id": run_id, "event_type": event_type, "payload": dict(payload)})


def warn_hooks_without_context_engine(engine: Any) -> None:
    """构造期告警：配置了 hooks 但没有 context_engine（on_context 无管线可挂）。

    只打一行 stderr：不抛错、不发事件——构造期没有 Run，任何事件都没有可归属
    的 run_id。on_provider_request/response 与 on_tool_result 仍需活跃 Run 才
    有归属，但它们的接线不依赖 context_engine，故照常生效。
    """
    if engine._hooks and engine._context_engine is None:
        print(
            "[ksadk.harness.hooks] hooks configured without context_engine: "
            "on_context hooks are inactive (no context pipeline); "
            "provider request/response and tool-result hooks remain active",
            file=sys.stderr,
        )


def bind_hooked_reasoner(engine: Any) -> None:
    """引擎构造期接线（langgraph.__init__ 收尾调用，保持引擎模块行数预算）。

    - hooks 非空：包装引擎 Reasoner。
    - hooks 为空但传入的 Reasoner 已是 _HookedReasoner（子 Agent 引擎复用父
      引擎的包装器，subagents.py 传 ``engine._reasoner``）：用子引擎重绑定，
      避免子 Agent 的模型调用被记到父 Run 名下；钩子链保持一致。
    - hooks 为空且未经包装：不包装，行为与现状逐字节相同（也不触碰
      ``_active_hook_run``——它只在引擎真正包装 Reasoner 后才有意义）。

    同时承担构造期的 hooks×context_engine 组合告警（见
    :func:`warn_hooks_without_context_engine`）：该告警属构造期接线事实，
    放在此处的唯一原因是 langgraph.py 处于 1000 行架构预算上限。
    """
    warn_hooks_without_context_engine(engine)
    reasoner = engine._reasoner
    if isinstance(reasoner, _HookedReasoner):
        engine._reasoner = reasoner.rebind(engine)
    elif engine._hooks:
        engine._reasoner = _HookedReasoner(reasoner, engine._hooks, engine)


def flush_hook_degradations(engine: Any, run: Any) -> int:
    """把本 Run 的缓冲降级事件落到 Run 事件流（``engine._event``，seq 单调）。

    调用时点：模型调用节点在合并完 loop 事件（``run.seq`` 已被正确推进）之后、
    以及模型调用失败的 except 路径（模型调用失败时降级事实同样不能丢）、
    还有 ``langgraph._execute`` 通用失败路径在追加 AGENT_COMPLETED/RUN_FAILED
    终态事件之前（该处 run.seq 已由各节点推进到最新，seq 安全）。
    其他 Run（并发/子 Agent）的缓冲项原样保留，等待它们自己的 flush。
    """
    buffer = getattr(engine, "_pending_hook_degradations", None)
    if not buffer:
        return 0
    run_id = str(run.handle.run_id)
    pending: list[dict[str, Any]] = []
    flushed = 0
    for entry in buffer:
        if entry["run_id"] != run_id:
            pending.append(entry)
            continue
        run.events.append(engine._event(run, entry["event_type"], dict(entry["payload"])))
        flushed += 1
    buffer[:] = pending
    return flushed


def clear_hook_run(engine: Any, run: Any) -> None:
    """Run 收尾清空活跃钩子上下文（仅当仍是本 Run，避免并发 Run 互相踩踏）。

    同时丢弃本 Run 未能 flush 的残留降级缓冲（图更早失败、模型调用节点未抵
    达时不让缓冲跨 Run 泄漏到手后 Run）——残留条目按「无法归属」记录 stderr。
    """
    if getattr(engine, "_active_hook_run", None) is run:
        engine._active_hook_run = None
    buffer = getattr(engine, "_pending_hook_degradations", None)
    if not buffer:
        return
    run_id = str(run.handle.run_id)
    pending = [entry for entry in buffer if entry["run_id"] != run_id]
    if len(pending) != len(buffer):
        print(
            "[ksadk.harness.hooks] provider hook degradations discarded at run end: "
            f"{len(buffer) - len(pending)} unflushed ({run_id})",
            file=sys.stderr,
        )
    buffer[:] = pending


__all__ = [
    "_HookedReasoner",
    "bind_hooked_reasoner",
    "buffer_hook_degradation",
    "clear_hook_run",
    "flush_hook_degradations",
    "warn_hooks_without_context_engine",
]
