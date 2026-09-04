"""Managed Engine 的 Context 构建管线（长任务方案 §6 / §8）。

从 ``engine/langgraph.py`` 抽出（模块体量守卫）：规划 → 主动/紧急压缩 →
组装 → ContextManifest/context.built 投影，以及压缩前受控 Memory Flush。
事件落账统一走引擎注入的 ``event_fn``（保证 seq 单调与信封一致）。
"""

from __future__ import annotations

from typing import Any, Callable

from ksadk.harness.compaction_record import build_compaction_record
from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.loop import ModelFailoverExhausted, ReasonInput, reason_turn_async
from ksadk.harness.state import Message, MessageRole

#: 事件构造函数签名（引擎的 _event）。
EventFn = Callable[..., RuntimeEvent]


class EngineContextPipeline:
    """ContextEngine 的运行时编排：规划、压缩、组装、Manifest 投影。"""

    def __init__(
        self,
        *,
        context_engine: Any,
        reasoner: Any,
        event_fn: EventFn,
        memory_runtime: Any | None = None,
        prompt_cache_tracker: Any | None = None,
    ) -> None:
        self._context_engine = context_engine
        self._reasoner = reasoner
        self._event = event_fn
        self._memory_runtime = memory_runtime
        self._prompt_cache_tracker = prompt_cache_tracker

    # ------------------------------------------------------------ 主入口

    async def prepare_context(self, run: Any, instructions: str) -> list[dict[str, Any]] | None:
        """ContextEngine 真正控制首次模型输入（收口 1）。

        规划 → 超过主动阈值（Spec context_policy）先主动压缩 → 仍超预算做一次
        紧急压缩（只允许一次，§8.4）→ 组装为 Chat 输入。未注入 ContextEngine 时
        返回 None（回退旧拼接路径）。
        """
        from ksadk.harness.context_engine import ContextEngineError

        history = run.request.metadata.get("conversation_history") or []
        messages = [
            Message(
                role=MessageRole(m.get("role", "user")),
                content=str(m.get("content") or ""),
                tool_call_id=str(m["tool_call_id"]) if m.get("tool_call_id") else None,
                name=str(m["name"]) if m.get("name") else None,
            )
            for m in history
            if isinstance(m, dict) and m.get("role") in {"user", "assistant", "system"}
        ]
        current_input = str(run.request.input or "")
        # Studio/Draft 会把本轮输入同时放进 request.input 和历史末尾。历史副本
        # 不能继续作为可丢弃 history_round 参与规划，否则超大本轮输入会在
        # Planner 限额时被静默裁掉。只移除重复副本，本轮输入始终以 required
        # current_input 单独进入 ContextPlan。
        if (
            current_input
            and messages
            and messages[-1].role is MessageRole.USER
            and messages[-1].content == current_input
        ):
            messages = messages[:-1]
        # 长任务方案 §9：recall_memory_and_knowledge——规划前检索长期 Memory，
        # 命中以 system 段注入（planner 按 history_system 高优先级保留）。
        messages = self._recall_memory(run, messages) + messages
        window, source = self._resolve_window(run)
        plan = self._plan_context(run, messages, current_input=current_input)
        self._emit_planned(run, plan, window, source)
        policy = run.compiled.spec.context_policy
        max_input = plan.budget.max_input_tokens

        if plan.planned_input_tokens > int(policy.proactive_compaction_threshold * max_input) or (
            # Planner 丢弃了历史轮次（静默丢上下文）→ 主动压缩以摘要保住事实。
            any(d.action == "dropped" and d.item_id.startswith("history") for d in plan.decisions)
        ):
            compacted = await self._compact_history(
                run, messages, trigger="proactive", keep_recent=6, budget_tokens=max_input
            )
            if compacted is not None:
                messages = compacted
                plan = self._plan_context(run, messages, current_input=current_input)
                self._emit_planned(run, plan, window, source)
                max_input = plan.budget.max_input_tokens

        if plan.planned_input_tokens > max_input:
            # 紧急压缩（§8.4）：更强压缩，只允许一次。
            compacted = await self._compact_history(
                run, messages, trigger="emergency", keep_recent=2, budget_tokens=max_input
            )
            if compacted is not None:
                messages = compacted
                plan = self._plan_context(run, messages, current_input=current_input)
                self._emit_planned(run, plan, window, source)
                run.events.append(
                    self._event(
                        run,
                        EventType.CONTEXT_RECOVERED,
                        {"reason": "context_overflow", "trigger": "emergency"},
                    )
                )
            if plan.planned_input_tokens > plan.budget.max_input_tokens:
                current_tokens = count_tokens(current_input)
                raise ContextEngineError(
                    "紧急压缩后仍超出输入预算；本轮用户输入不会被静默丢弃: "
                    f"planned_input_tokens={plan.planned_input_tokens}, "
                    f"current_input_tokens={current_tokens}, "
                    f"max_input_tokens={plan.budget.max_input_tokens}"
                )

        assembled = self._context_engine.assemble_chat(plan)
        if not assembled.messages or assembled.messages[0].get("role") != "system":
            assembled.messages.insert(0, {"role": "system", "content": instructions})
        # 长任务方案 §6.2：Manifest 可观测快照 + context.built（Planned/Projected；
        # Actual 由 usage 回填）。正文不入 Manifest，只记 Hash/引用/Token。
        projected = sum(count_tokens(str(m.get("content") or "")) for m in assembled.messages)
        self._emit_context_built(run, plan, projected, window)
        return assembled.messages

    async def recover_model_overflow(
        self,
        run: Any,
        messages: list[dict[str, Any]],
        instructions: str,
    ) -> list[dict[str, Any]]:
        """模型明确拒绝上下文后执行一次更强压缩并重建输入。

        这是 provider 真实 overflow 的恢复路径，不是首次调用前的预算预测。
        重试预算保存在 HarnessState，避免模型/备用模型之间形成无限循环；
        最近 Tool Call/Result 作为完整尾部原样保留，不能被重建成残缺消息。
        """
        from ksadk.harness.context_engine import ContextEngineError

        retries = run.state.retry_state.emergency_compaction_retries
        limit = run.compiled.spec.context_policy.emergency_retry_limit
        if retries >= limit:
            raise ContextEngineError(
                f"模型上下文溢出已执行 {retries} 次紧急压缩，达到上限 {limit}"
            )

        # Stable Prompt 是高信任、不参与摘要的固定前缀；只压缩其后的动态消息。
        stable_prefix: list[dict[str, Any]] = []
        dynamic_messages = messages
        if messages and str(messages[0].get("role") or "") == "system":
            stable_prefix = [dict(messages[0])]
            dynamic_messages = messages[1:]
        converted = [_chat_message(message) for message in dynamic_messages]
        # 保留至少最后两条，并把边界向前移动到 Tool Pair 之前。
        cutoff = max(0, len(dynamic_messages) - 2)
        while cutoff > 0 and str(dynamic_messages[cutoff].get("role") or "") == "tool":
            cutoff -= 1
        if cutoff <= 0:
            raise ContextEngineError("上下文溢出但没有可继续压缩的历史消息")

        run.state.retry_state = run.state.retry_state.model_copy(
            update={
                "emergency_compaction_retries": retries + 1,
                "last_error_kind": "context_length",
            }
        )
        compacted = await self._compact_history(
            run,
            converted,
            trigger="emergency",
            keep_recent=len(dynamic_messages) - cutoff,
            budget_tokens=self._plan_context(run, converted).budget.max_input_tokens,
        )
        if compacted is None:
            raise ContextEngineError("上下文溢出但紧急压缩没有产生新输入")

        summary = compacted[0].content
        recovered: list[dict[str, Any]] = list(stable_prefix)
        recovered.append({"role": "system", "content": summary})
        recovered.extend(dict(message) for message in dynamic_messages[cutoff:])
        if not recovered or recovered[0].get("role") != "system":
            recovered.insert(0, {"role": "system", "content": instructions})

        # 为恢复后的真实模型输入生成新的 Manifest 世代；正文仍不落 Manifest。
        recovered_state = [_chat_message(message) for message in recovered[len(stable_prefix) :]]
        plan = self._plan_context(run, recovered_state)
        window, _source = self._resolve_window(run)
        projected = sum(count_tokens(str(m.get("content") or "")) for m in recovered)
        self._emit_context_built(run, plan, projected, window)
        run.events.append(
            self._event(
                run,
                EventType.CONTEXT_RECOVERED,
                {
                    "reason": "provider_context_overflow",
                    "trigger": "emergency",
                    "retry": retries + 1,
                    "retry_limit": limit,
                    "message_count_before": len(messages),
                    "message_count_after": len(recovered),
                },
            )
        )
        return recovered

    # ------------------------------------------------------------ Memory 召回

    def _recall_memory(self, run: Any, messages: list[Message]) -> list[Message]:
        """检索长期 Memory 并以 system 段注入（长任务方案 §7.5 / §9）。

        - 检索前先做 scope 允许列表过滤（policy 未启用 → 不检索）；
        - 每条命中在 ``memory.recalled`` 事件中给出 memory_id/score/injected
          （injected = 最终被 planner 选入；由 context.built 的 sections
          佐证，这里记召回集合本身）；
        - Provider 故障返回空结果（HarnessMemoryRuntime 语义），不污染输入。
        """
        if self._memory_runtime is None or not run.compiled.spec.memory_policy.enabled:
            return []
        query = str(run.request.input or "").strip()
        if not query:
            return []

        def _scope_id(scope: str) -> str:
            if scope == "agent":
                return f"agent:{run.state.agent_id}"
            return f"user:{run.state.user_id or 'anonymous'}"

        scopes = [
            (scope, _scope_id(scope))
            for scope in ("agent", "user")
            if scope in set(run.compiled.spec.memory_policy.scopes)
        ]
        if not scopes:
            return []
        result = self._memory_runtime.recall(query=query, scopes=scopes)
        if result.status != "ok" or not result.records:
            return []
        items = []
        lines: list[str] = []
        for record in result.records:
            items.append(
                {
                    "memory_id": record.memory_id,
                    "scope": record.scope,
                    "score": _keyword_score(query, record.content),
                    "injected": True,
                }
            )
            lines.append(f"- （长期记忆 {record.memory_id}）{record.summary or record.content}")
        run.events.append(
            self._event(
                run,
                EventType.MEMORY_RECALLED,
                {
                    "scope": ",".join(s for s, _ in scopes),
                    "query": query[:256],
                    "items": items,
                },
            )
        )
        return [Message(role=MessageRole.SYSTEM, content="【相关长期记忆】\n" + "\n".join(lines))]

    # ------------------------------------------------------------ 规划

    def _resolve_window(self, run: Any) -> tuple[int, str]:
        from ksadk.harness.context_engine import resolve_context_window

        raw_window = run.request.metadata.get("context_window_tokens")
        return resolve_context_window(
            model_profile_window=int(raw_window) if isinstance(raw_window, (int, float)) else None
        )

    def _plan_context(
        self,
        run: Any,
        messages: list[Message],
        *,
        current_input: str | None = None,
    ) -> Any:
        """对给定历史做一次上下文规划（ContextRequest 以快照构建）。"""
        from ksadk.harness.context_engine import ContextRequest

        window, _source = self._resolve_window(run)
        snapshot = run.state.model_copy(update={"messages": messages})
        effective_input = str(run.request.input or "") if current_input is None else current_input
        return self._context_engine.plan(
            ContextRequest(
                spec=run.compiled.spec,
                state=snapshot,
                user_input=effective_input,
                context_window_tokens=window,
                skill_catalog=tuple(run.skill_catalog),
            )
        )

    def _emit_planned(self, run: Any, plan: Any, window: int, source: str) -> None:
        run.events.append(
            self._event(
                run,
                EventType.CONTEXT_PLANNED,
                {
                    "budget_tokens": plan.budget.max_input_tokens,
                    "sections": dict(plan.tokens_by_kind),
                    "window_source": source,
                    "context_window_tokens": window,
                    "planned_input_tokens": plan.planned_input_tokens,
                },
            )
        )

    def _emit_context_built(self, run: Any, plan: Any, projected: int, window: int) -> None:
        from ksadk.harness.context_manifest import build_manifest

        previous = run.context_manifest
        manifest = build_manifest(
            plan=plan,
            projected_tokens=projected,
            run_id=run.handle.run_id,
            scope_id=f"agent:{run.state.agent_id}",
            model_profile_ref=run.compiled.spec.model.profile_ref,
            supersedes=previous.manifest_id if previous is not None else None,
        )
        run.context_manifest = manifest
        sections = {
            section.item_id: section.estimated_tokens
            for section in manifest.sections
            if section.included
        }
        run.events.append(
            self._event(
                run,
                EventType.CONTEXT_BUILT,
                {
                    "manifest_id": manifest.manifest_id,
                    "stable_prompt_hash": manifest.stable_prompt_hash,
                    "planned_tokens": manifest.planned_tokens,
                    "projected_tokens": manifest.projected_tokens,
                    "context_window_tokens": window,
                    "sections": sections,
                    "supersedes": manifest.supersedes or "",
                },
            )
        )
        if self._prompt_cache_tracker is not None:
            diagnostic = self._prompt_cache_tracker.observe(
                scope=(
                    f"{run.state.tenant_id}:{run.state.agent_id}:"
                    f"{run.compiled.spec.model.profile_ref}"
                ),
                stable_prompt_hash=manifest.stable_prompt_hash,
            )
            run.events.append(
                self._event(
                    run,
                    EventType.PROMPT_CACHE_DIAGNOSTIC,
                    {
                        **diagnostic.to_payload(),
                        "manifest_id": manifest.manifest_id,
                    },
                )
            )

    # ------------------------------------------------------------ 压缩

    async def _summarize(self, run: Any, head: list[Message]) -> str:
        """受控摘要调用：走统一 reason 通道（事件成对、usage 可审计）。失败降级拼接。"""
        spec = run.compiled.spec
        try:
            out = await reason_turn_async(
                0,
                ReasonInput(
                    model_ref=spec.model.profile_ref,
                    fallback_model_refs=spec.model.fallback_profile_refs,
                    provider_policy=spec.model.provider_policy,
                    instructions=(
                        "你是上下文压缩器。请把以下对话历史压缩为要点摘要，"
                        "必须保留所有 ID、金额、日期、审批号等关键事实。"
                    ),
                    messages=[{"role": "user", "content": "\n".join(m.content for m in head)}],
                    tools=[],
                    reasoner=self._reasoner,
                    agent_id=run.state.agent_id,
                    user_id=run.state.user_id,
                    session_id=run.state.session_id,
                    run_id=run.handle.run_id,
                    seq_start=run.seq,
                    max_turns=1,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 摘要失败降级为截断拼接，不阻断 Run
            if isinstance(exc, ModelFailoverExhausted):
                for event in exc.events:
                    run.events.append(event)
                    run.seq = max(run.seq, event.seq_id)
            text = "\n".join(m.content for m in head)
            # 降级不静默（§6.4 "不静默丢弃"）：真实模型评测曾把摘要失败
            # 吞成 0-token Run；发 context.recovered 让降级可观测可告警。
            run.events.append(
                self._event(
                    run,
                    EventType.CONTEXT_RECOVERED,
                    {
                        "reason": "compaction_summary_failed:degraded_truncation",
                        "error": str(exc)[:256],
                    },
                )
            )
            return text[:2000] + ("\n…[截断]" if len(text) > 2000 else "")
        for ev in out.events:
            # 压缩摘要是管线内部模型调用，不对应任何 ContextManifest；
            # usage 事件标记 purpose=compaction（真实模型评测暴露：否则
            # Actual↔Manifest 配对率虚低，且压缩花费不可归因）。
            if ev.event_type == EventType.USAGE_REPORTED:
                ev.payload.setdefault("purpose", "compaction")
            run.events.append(ev)
            run.seq = max(run.seq, ev.seq_id)
        return str(out.new_messages[0].get("content") or "") if out.new_messages else ""

    async def _compact_history(
        self,
        run: Any,
        messages: list[Message],
        *,
        trigger: str,
        keep_recent: int,
        budget_tokens: int,
    ) -> list[Message] | None:
        """主动/紧急压缩：摘要 head + 保留 tail + 关键事实重注入，返回新历史。"""
        from ksadk.harness.context_engine import CompactionRequest

        if len(messages) <= keep_recent:
            return None
        run.events.append(
            self._event(
                run,
                EventType.CONTEXT_COMPACTION_STARTED,
                {"phase": "before", "trigger": trigger},
            )
        )
        # 长任务方案 §6.4 主动压缩步骤 2：压缩前受控 Memory Flush——
        # 被压缩历史先经提取管线落长期 Memory（审计事件并入事件流）。
        memory_candidate_refs = self._flush_memory_before_compaction(run, messages[:-keep_recent])
        before_tokens = sum(count_tokens(m.content) for m in messages)
        summary = await self._summarize(run, messages[:-keep_recent])
        checkpoint = self._context_engine.compact(
            CompactionRequest(
                messages=messages,
                keep_recent=keep_recent,
                trigger=trigger,
                summary=summary,
                compacted_count=len(messages) - keep_recent,
            )
        )
        compacted_text = (checkpoint.summary or "").strip()
        if checkpoint.reinjection:
            compacted_text = (compacted_text + "\n\n" + checkpoint.reinjection).strip()
        new_messages: list[Message] = [
            Message(role=MessageRole.SYSTEM, content=f"【历史摘要】\n{compacted_text}")
        ]
        new_messages.extend(messages[-keep_recent:])
        after_tokens = sum(count_tokens(m.content) for m in new_messages)
        # 长任务方案 §6.4：CompactionRecord（投影变化记录，非新事实源）。
        # 摘要缺失但经重注入保留的事实不算最终丢弃（§8.4.1 "不静默丢弃"）。
        reinjected = (
            tuple(checkpoint.dropped_critical_facts) if checkpoint.reinjection else ()
        )
        record = build_compaction_record(
            run_id=run.handle.run_id,
            trigger=trigger,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            compacted_event_range=(0, int(checkpoint.compacted_until_seq_id)),
            summary=compacted_text,
            retained_critical_facts=tuple(checkpoint.retained_critical_facts),
            dropped_critical_facts=tuple(checkpoint.dropped_critical_facts),
            reinjected_critical_facts=reinjected,
            summary_model_ref=run.compiled.spec.model.profile_ref,
            memory_candidate_refs=tuple(memory_candidate_refs),
        )
        run.compaction_records.append(record)
        run.events.append(
            self._event(
                run,
                EventType.CONTEXT_COMPACTION_COMPLETED,
                {
                    "phase": "after",
                    "trigger": trigger,
                    "compacted_until_seq_id": checkpoint.compacted_until_seq_id,
                    "budget_tokens": budget_tokens,
                    "dropped_critical_facts": list(checkpoint.dropped_critical_facts),
                    "reinjected_critical_facts": list(reinjected),
                    # 长任务方案 §8：Compaction 投影（引用 + 前后 Token + 质量校验）。
                    "compaction_id": record.compaction_id,
                    "before_tokens": record.before_tokens,
                    "after_tokens": record.after_tokens,
                    "quality_checks": record.quality_checks(),
                    "memory_candidate_refs": list(record.memory_candidate_refs),
                },
            )
        )
        return new_messages

    def _flush_memory_before_compaction(self, run: Any, head: list[Message]) -> list[str]:
        """压缩前受控 Memory Flush（§6.4 步骤 2）。

        未注入 MemoryRuntime 或 policy 未启用时为 no-op；审计事件并入
        Run 事件流（memory.write / memory.conflict）。返回已提交候选引用。
        """
        if self._memory_runtime is None or not run.compiled.spec.memory_policy.enabled:
            return []
        from ksadk.harness.memory_loop import write_back_messages

        committed: list[str] = []

        def _sink(event: RuntimeEvent) -> None:
            if event.payload.get("decision") == "commit":
                committed.append(str(event.payload.get("memory_ref") or ""))
            run.events.append(self._event(run, event.event_type, dict(event.payload)))

        try:
            write_back_messages(
                self._memory_runtime,
                run.compiled.spec,
                run_id=run.handle.run_id,
                agent_id=run.state.agent_id,
                user_id=run.state.user_id,
                messages=head,
                audit_sink=_sink,
            )
        except Exception:  # noqa: BLE001 - Flush 是后置增强，失败不阻断压缩
            run.events.append(
                self._event(
                    run,
                    EventType.MEMORY_WRITE,
                    {"scope": "agent", "memory_ref": "", "decision": "flush_failed"},
                )
            )
        return [ref for ref in committed if ref]


def count_tokens(text: str) -> int:
    from ksadk.context_engine.tokenizer import get_default_token_counter

    return get_default_token_counter().count_text(text)


def _chat_message(message: dict[str, Any]) -> Message:
    """把 OpenAI 消息投影为压缩/规划使用的稳定 Message 形态。"""
    role_value = str(message.get("role") or "user")
    try:
        role = MessageRole(role_value)
    except ValueError:
        role = MessageRole.USER
    tool_call_id = str(message.get("tool_call_id") or "") or None
    if role is MessageRole.ASSISTANT and not tool_call_id:
        calls = message.get("tool_calls")
        if isinstance(calls, list) and calls and isinstance(calls[0], dict):
            tool_call_id = str(calls[0].get("id") or "") or None
    return Message(
        role=role,
        content=str(message.get("content") or ""),
        tool_call_id=tool_call_id,
        name=str(message.get("name") or "") or None,
    )


def _keyword_score(query: str, content: str) -> float:
    """廉价关键词命中率（0~1）：供 memory.recalled 的 items.score 观测。"""
    terms = [t for t in query.split() if len(t) >= 2]
    if not terms:
        return 0.0
    hits = sum(1 for t in terms if t in content)
    return round(hits / len(terms), 4)


__all__ = ["EngineContextPipeline", "count_tokens"]
