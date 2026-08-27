"""Managed Engine 的 Context 构建管线（长任务方案 §6 / §8）。

从 ``engine/langgraph.py`` 抽出（模块体量守卫）：规划 → 主动/紧急压缩 →
组装 → ContextManifest/context.built 投影，以及压缩前受控 Memory Flush。
事件落账统一走引擎注入的 ``event_fn``（保证 seq 单调与信封一致）。
"""

from __future__ import annotations

from typing import Any, Callable

from ksadk.harness.compaction_record import build_compaction_record
from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.loop import ReasonInput, reason_turn_async
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
    ) -> None:
        self._context_engine = context_engine
        self._reasoner = reasoner
        self._event = event_fn
        self._memory_runtime = memory_runtime

    # ------------------------------------------------------------ 主入口

    async def prepare_context(
        self, run: Any, instructions: str
    ) -> list[dict[str, Any]] | None:
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
        window, source = self._resolve_window(run)
        plan = self._plan_context(run, messages)
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
                plan = self._plan_context(run, messages)
                self._emit_planned(run, plan, window, source)
                max_input = plan.budget.max_input_tokens

        if plan.planned_input_tokens > max_input:
            # 紧急压缩（§8.4）：更强压缩，只允许一次。
            compacted = await self._compact_history(
                run, messages, trigger="emergency", keep_recent=2, budget_tokens=max_input
            )
            if compacted is not None:
                messages = compacted
                plan = self._plan_context(run, messages)
                self._emit_planned(run, plan, window, source)
                run.events.append(
                    self._event(
                        run,
                        EventType.CONTEXT_RECOVERED,
                        {"reason": "context_overflow", "trigger": "emergency"},
                    )
                )
            if plan.planned_input_tokens > plan.budget.max_input_tokens:
                raise ContextEngineError(
                    "紧急压缩后仍超出输入预算: "
                    f"{plan.planned_input_tokens} > {plan.budget.max_input_tokens}"
                )

        assembled = self._context_engine.assemble_chat(plan)
        if not assembled.messages or assembled.messages[0].get("role") != "system":
            assembled.messages.insert(0, {"role": "system", "content": instructions})
        # 长任务方案 §6.2：Manifest 可观测快照 + context.built（Planned/Projected；
        # Actual 由 usage 回填）。正文不入 Manifest，只记 Hash/引用/Token。
        projected = sum(
            count_tokens(str(m.get("content") or "")) for m in assembled.messages
        )
        self._emit_context_built(run, plan, projected, window)
        return assembled.messages

    # ------------------------------------------------------------ 规划

    def _resolve_window(self, run: Any) -> tuple[int, str]:
        from ksadk.harness.context_engine import resolve_context_window

        raw_window = run.request.metadata.get("context_window_tokens")
        return resolve_context_window(
            model_profile_window=int(raw_window) if isinstance(raw_window, (int, float)) else None
        )

    def _plan_context(self, run: Any, messages: list[Message]) -> Any:
        """对给定历史做一次上下文规划（ContextRequest 以快照构建）。"""
        from ksadk.harness.context_engine import ContextRequest

        window, _source = self._resolve_window(run)
        snapshot = run.state.model_copy(update={"messages": messages})
        return self._context_engine.plan(
            ContextRequest(
                spec=run.compiled.spec,
                state=snapshot,
                user_input=str(run.request.input or ""),
                context_window_tokens=window,
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

    def _emit_context_built(
        self, run: Any, plan: Any, projected: int, window: int
    ) -> None:
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

    # ------------------------------------------------------------ 压缩

    async def _summarize(self, run: Any, head: list[Message]) -> str:
        """受控摘要调用：走统一 reason 通道（事件成对、usage 可审计）。失败降级拼接。"""
        spec = run.compiled.spec
        try:
            out = await reason_turn_async(
                0,
                ReasonInput(
                    model_ref=spec.model.profile_ref,
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
        except Exception:  # noqa: BLE001 - 摘要失败降级为截断拼接，不阻断 Run
            text = "\n".join(m.content for m in head)
            return text[:2000] + ("\n…[截断]" if len(text) > 2000 else "")
        for ev in out.events:
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
        record = build_compaction_record(
            run_id=run.handle.run_id,
            trigger=trigger,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            compacted_event_range=(0, int(checkpoint.compacted_until_seq_id)),
            summary=compacted_text,
            retained_critical_facts=tuple(checkpoint.retained_critical_facts),
            dropped_critical_facts=tuple(checkpoint.dropped_critical_facts),
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


__all__ = ["EngineContextPipeline", "count_tokens"]
