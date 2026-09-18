"""Adaptive child-task delegation for the managed Harness provider.

The parent model sees one stable ``delegate_task`` tool instead of one tool per
predeclared child.  The platform then chooses one of the only two supported
child providers:

* coding and repository modification tasks -> Codex;
* research, analysis and other general tasks -> managed KsADK Harness.

This keeps the topology dynamic (zero to many children) while making provider
selection, lineage and progress observable rather than prompt-only behaviour.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Literal

from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.public_activity import public_commentary_text
from ksadk.harness.subagent import SubAgentSpec, run_subagent
from ksadk.plugins.subagent_providers.codex import DEFAULT_CODEX_CHILD_PROVIDER_REF
from ksadk.plugins.subagents import (
    ChildHandle,
    SpawnSubagentRequest,
    SubagentPolicy,
    SubagentProviderError,
    SubagentProviderRouter,
)

DELEGATE_TASK_TOOL = "delegate_task"
HARNESS_CHILD_PROVIDER_REF = "plugin://io.ksadk.harness-child@1.0.0"

_CODING_SIGNALS = re.compile(
    r"(?:\b(?:implement|refactor|debug|fix|patch|modify|edit)\s+"
    r"(?:(?:the|this|a|an)\s+)?(?:code|files?|repository|repo|bug|tests?|"
    r"function|method|class|module|feature|api|script|program|implementation)\b|"
    r"\bcommit\s+(?:(?:the|this)\s+)?(?:changes?|patch|code)\b|"
    r"\bwrite\s+(?:the\s+)?(?:code|tests?|implementation)\b|"
    r"\badd\s+(?:unit\s+|integration\s+)?tests?\b|"
    r"(?:编写|重构|调试|修复|修改)\s*"
    r"(?:(?:Python|TypeScript|JavaScript|Java|Go|Rust|C\+\+|C#|SQL)\s*)?(?:代码|程序|脚本)|"
    r"写代码|实现(?:功能|需求|接口|类|方法|模块)|"
    r"补测试|修改代码|提交代码|改文件|修改文件)",
    re.IGNORECASE,
)
_NEGATED_CODING_SIGNALS = re.compile(
    r"(?:\b(?:do\s+not|don't|never|without)\s+"
    r"(?:write|modify|edit|change|create)\s+(?:any\s+)?(?:code|files?|scripts?)\b|"
    r"(?:不要|无需|不得|禁止)(?:编写|写|修改|编辑|创建)(?:任何)?(?:代码|文件|脚本)"
    r"(?:(?:或|、)(?:编写|写|修改|编辑|创建)(?:任何)?(?:代码|文件|脚本))*)",
    re.IGNORECASE,
)
_DIRECT_FILE_DELIVERY_SIGNALS = re.compile(
    r"(?:创建|生成|保存|写入|输出)(?:一个|一份|为)?[^。；\n]{0,80}"
    r"(?:\.(?:md|txt|json|csv|html?)\b|(?:Markdown|MD|文本|JSON|CSV|HTML)\s*文档|文件)|"
    r"\b(?:create|generate|save|write)\b[^.\n]{0,80}"
    r"(?:\.(?:md|txt|json|csv|html?)\b|\b(?:markdown|text|json|csv|html)\s+file\b)",
    re.IGNORECASE,
)
_RESEARCH_SIGNALS = re.compile(
    r"(?:调研|研究|检索|搜索|查找|核对|资料|证据|比较|对比|"
    r"\b(?:research|investigate|search|compare|verify)\b)",
    re.IGNORECASE,
)
_RESEARCH_LEAD = re.compile(
    r"^\s*(?:请\s*)?(?:调研|研究|检索|搜索|查找|核对|比较|对比|"
    r"(?:research|investigate|search|compare|verify)\b)",
    re.IGNORECASE,
)
_MUTATING_TASK_SIGNALS = re.compile(
    r"^\s*(?:请\s*|please\s+)?(?:将|把)?[^。；\n]{0,40}"
    r"(?:保存|写入|编辑|修改|上传|发布|删除|"
    r"\b(?:save|write|edit|modify|upload|publish|delete)\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DelegationDecision:
    task_kind: Literal["coding", "general"]
    provider_ref: str
    reason: str


class AdaptiveDelegationRuntime:
    """Route dynamically requested child tasks to Harness or Codex."""

    name = DELEGATE_TASK_TOOL

    def __init__(
        self,
        *,
        router: SubagentProviderRouter | None = None,
        codex_available: bool = False,
        max_children: int = 8,
        child_timeout_seconds: int = 300,
        child_max_turns: int = 8,
        child_max_total_tokens: int | None = None,
    ) -> None:
        if not 1 <= max_children <= 32:
            raise ValueError("dynamic delegation max_children must be in 1..32")
        self._router = router
        self._codex_available = codex_available
        self._max_children = max_children
        self._child_timeout_seconds = child_timeout_seconds
        self._child_max_turns = child_max_turns
        self._child_max_total_tokens = child_max_total_tokens
        self._active: dict[str, dict[str, ChildHandle]] = {}
        self._started: dict[str, int] = {}

    @property
    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": (
                    "Delegate one independent subtask when parallel or specialist work is useful. "
                    "Use task_kind=coding for repository/code implementation, debugging, "
                    "refactoring or tests; use general for research and analysis. When a request "
                    "contains N independent subjects, emit N delegate_task calls in the same "
                    "model turn so they run in parallel; never delegate only the first subject "
                    "and process the rest serially. After successful delegation, synthesize the "
                    "child outputs; do not delegate creating or saving the final deliverable "
                    "file—the parent must use its workspace write tool after synthesis; do not "
                    "repeat the same research with parent tools unless a child explicitly "
                    "reports missing evidence. Do not delegate a simple task that you can "
                    "answer directly."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "A self-contained child task with expected output.",
                        },
                        "task_kind": {
                            "type": "string",
                            "enum": ["auto", "coding", "general"],
                            "default": "auto",
                        },
                        "label": {
                            "type": "string",
                            "description": "Short user-visible stage or child label.",
                        },
                    },
                    "required": ["task"],
                    "additionalProperties": False,
                },
            },
        }

    def route(self, task: str, requested_kind: str = "auto") -> DelegationDecision:
        requested = requested_kind if requested_kind in {"auto", "coding", "general"} else "auto"
        # Research prompts frequently contain safety constraints such as
        # "Do not write any code or files".  Those negative phrases describe
        # what the child must avoid and must not escalate a general research
        # task to the Codex coding provider.
        routing_text = _NEGATED_CODING_SIGNALS.sub("", task)
        coding_signal = bool(_CODING_SIGNALS.search(routing_text))
        # The platform is authoritative: a model cannot label an obviously coding
        # task as general merely to bypass the Codex sandbox/provider boundary.
        # ``task_kind`` is only a model hint. Provider selection is a platform
        # policy boundary: mentioning or researching Codex must not launch a
        # coding runner, and a model cannot escalate a general task by simply
        # labelling it ``coding``. Explicit repository/code intent is required.
        coding = coding_signal
        if coding:
            return DelegationDecision(
                task_kind="coding",
                provider_ref=DEFAULT_CODEX_CHILD_PROVIDER_REF,
                reason="coding_signal",
            )
        return DelegationDecision(
            task_kind="general",
            provider_ref=HARNESS_CHILD_PROVIDER_REF,
            reason=(
                "coding_hint_without_signal"
                if requested == "coding"
                else "explicit_general_kind" if requested == "general" else "general_default"
            ),
        )

    def is_tool(self, name: str) -> bool:
        return name == self.name

    def parallel_safe(self, arguments: dict[str, Any], *, parent_run: Any) -> bool:
        """Whether an adaptive child can safely share a parallel tool batch.

        Research children never need the parent's mutating/approval-gated tools:
        they return evidence to the parent, which owns the final artifact.  By
        removing those tools from the child we avoid concurrent approval
        interrupts while allowing independent product research to run in
        parallel.  Other general tasks retain the existing sequential,
        approval-capable behaviour.
        """
        task = str(arguments.get("task") or "").strip()
        if (
            not task
            or self.route(task, str(arguments.get("task_kind") or "auto")).task_kind
            != "general"
        ):
            return False
        if not _RESEARCH_SIGNALS.search(task):
            return False
        mutating = (
            bool(_DIRECT_FILE_DELIVERY_SIGNALS.search(task))
            if _RESEARCH_LEAD.search(task)
            else bool(_MUTATING_TASK_SIGNALS.search(task))
        )
        if mutating:
            return False
        return not (
            bool(getattr(parent_run, "skill_catalog", ()))
            or bool(getattr(parent_run, "mcp_catalog", ()))
        )

    def preview_route(self, arguments: dict[str, Any], *, call_id: str) -> dict[str, Any]:
        """Return the public route decision before the child starts."""
        task = str(arguments.get("task") or "").strip()
        decision = self.route(task, str(arguments.get("task_kind") or "auto"))
        return {
            "kind": "delegation.route",
            "status": "in_progress",
            "call_id": call_id,
            "label": self.public_label(arguments, call_id=call_id),
            "task_kind": decision.task_kind,
            "provider_ref": decision.provider_ref,
            "reason": decision.reason,
        }

    async def invoke(
        self,
        *,
        engine: Any,
        parent_run: Any,
        arguments: dict[str, Any],
        call_id: str,
    ) -> tuple[dict[str, Any], list[RuntimeEvent]]:
        task = str(arguments.get("task") or "").strip()
        if not task:
            raise ValueError("delegate_task requires a non-empty task")
        # The parent owns final artifact delivery. A child conclusion is only
        # evidence for synthesis; allowing the model to delegate a direct file
        # write lets a text-only child claim success without any durable write
        # receipt. Fail before spawning so the next parent turn can call the
        # bound workspace tool itself.
        if _DIRECT_FILE_DELIVERY_SIGNALS.search(task):
            raise SubagentProviderError(
                "delegation_requires_parent_tool",
                "final file delivery must use the parent workspace write tool",
            )
        run_id = parent_run.handle.run_id
        calls = getattr(parent_run, "dynamic_calls", None)
        if calls is None:
            calls = set()
            parent_run.dynamic_calls = calls
        if call_id not in calls and len(calls) >= self._max_children:
            raise SubagentProviderError(
                "subagent_limit_exceeded",
                f"dynamic child limit {self._max_children} exhausted",
            )
        calls.add(call_id)
        self._started[run_id] = len(calls)
        decision = self.route(task, str(arguments.get("task_kind") or "auto"))
        label = self.public_label(arguments, call_id=call_id)
        if decision.task_kind == "coding" and getattr(
            parent_run, "execution_policy", None
        ) is not None:
            raise SubagentProviderError(
                "execution_policy_unsupported",
                "Codex child Provider cannot enforce this host execution policy",
            )
        if decision.task_kind == "coding" and (
            not self._codex_available or self._router is None
        ):
            raise SubagentProviderError(
                "codex_subagent_unavailable",
                "coding task requires the Codex child provider, but it is unavailable",
            )
        self._publish_progress(
            parent_run,
            status="running",
            call_id=call_id,
            label=label,
            provider_ref=decision.provider_ref,
            child_event_kind="lifecycle",
        )
        try:
            if decision.task_kind == "general":
                result, events = await self._run_harness(
                    engine=engine,
                    parent_run=parent_run,
                    task=task,
                    label=label,
                    call_id=call_id,
                )
            else:
                result, events = await self._run_codex(
                    parent_run=parent_run,
                    task=task,
                    label=label,
                    call_id=call_id,
                )
        except asyncio.CancelledError:
            self._publish_progress(
                parent_run,
                status="cancelled",
                call_id=call_id,
                label=label,
                provider_ref=decision.provider_ref,
                child_event_kind="terminal",
            )
            raise
        except Exception:
            self._publish_progress(
                parent_run,
                status="failed",
                call_id=call_id,
                label=label,
                provider_ref=decision.provider_ref,
                child_event_kind="terminal",
            )
            raise
        if decision.task_kind == "general":
            if result.get("status") != "completed":
                from ksadk.harness.subagent import SubAgentExecutionError

                self._publish_progress(
                    parent_run,
                    status="failed",
                    call_id=call_id,
                    label=label,
                    provider_ref=decision.provider_ref,
                    child_event_kind="terminal",
                )
                raise SubAgentExecutionError(
                    result.get("error_category") or "child_failed",
                    result.get("error") or "Harness child failed",
                )
            self._publish_progress(
                parent_run,
                status="succeeded",
                call_id=call_id,
                label=label,
                provider_ref=decision.provider_ref,
                child_event_kind="terminal",
                **(
                    {"public_summary": summary}
                    if (summary := self._public_result_summary(result))
                    else {}
                ),
            )
        else:
            self._publish_progress(
                parent_run,
                status="succeeded",
                call_id=call_id,
                label=label,
                provider_ref=decision.provider_ref,
                child_event_kind="terminal",
                **(
                    {"public_summary": summary}
                    if (summary := self._public_result_summary(result))
                    else {}
                ),
            )
        return (
            self._parent_result(
                result=result,
                provider="codex" if decision.task_kind == "coding" else "harness",
                task_kind=decision.task_kind,
                label=label,
            ),
            events,
        )

    @staticmethod
    def _parent_result(
        *,
        result: Any,
        provider: str,
        task_kind: str,
        label: str,
    ) -> dict[str, Any]:
        """Return only the child conclusion needed by the parent model.

        Harness child results also contain audit evidence, usage counters and
        internal execution metadata. Those details remain available through
        the child event stream; copying them into the parent tool message made
        synthesis prompts unnecessarily large and exposed implementation
        details to the model. Keep the parent-facing contract compact and
        stable instead.
        """
        output = result
        artifact_refs: tuple[str, ...] = ()
        if isinstance(result, dict):
            output = result.get("output")
            refs = result.get("artifact_refs") or ()
            if isinstance(refs, (list, tuple)):
                artifact_refs = tuple(str(ref) for ref in refs if str(ref).strip())
        payload: dict[str, Any] = {
            "status": "completed",
            "provider": provider,
            "task_kind": task_kind,
            "label": label,
            "output": output,
        }
        if artifact_refs:
            payload["artifact_refs"] = artifact_refs
        return payload

    @staticmethod
    def _public_result_summary(result: Any) -> str:
        """Extract only a child's public conclusion, never its trace metadata."""
        output = result.get("output") if isinstance(result, dict) else result
        return public_commentary_text(output, limit=220)

    async def _run_harness(
        self,
        *,
        engine: Any,
        parent_run: Any,
        task: str,
        label: str,
        call_id: str,
    ) -> tuple[Any, list[RuntimeEvent]]:
        child_name = self._child_name("harness", call_id)
        child_tools = tuple(name for name in parent_run.tools if name != self.name)
        if self.parallel_safe({"task": task, "task_kind": "general"}, parent_run=parent_run):
            approval_required = set(getattr(parent_run, "approval_required", ()))
            child_tools = tuple(name for name in child_tools if name not in approval_required)
        sub = SubAgentSpec(
            name=child_name,
            description=label,
            instructions=(
                "你是由 KsADK Harness 动态调度的通用子 Agent。只完成收到的独立子任务，"
                "使用可用证据，明确未验证内容，并把可供父 Agent 汇总的结论返回。"
                "调研任务应优先使用官方一手资料；当六条以内的高质量证据已足以回答时，"
                "立即停止继续搜索并形成结论。不得为了穷尽资料重复检索，最后一轮必须"
                "直接返回当前最佳结论以及仍未验证的事项。输出控制在 1200 中文字以内，"
                "避免替父 Agent 重复撰写完整报告。"
            ),
            tools=child_tools,
            timeout_seconds=float(self._child_timeout_seconds),
            max_turns=self._child_max_turns,
            max_total_tokens=self._child_max_total_tokens,
            inherit_skills=True,
            inherit_mcp=True,
            failure_policy="return_error",
        )
        result, _ = await run_subagent(
            engine=engine,
            parent_run=parent_run,
            sub=sub,
            task=task,
            call_id=call_id,
        )
        # Native children are already streamed into the parent audit sequence.
        # Returning those events again would double count Usage and lifecycle.
        return result, []

    async def _run_codex(
        self,
        *,
        parent_run: Any,
        task: str,
        label: str,
        call_id: str,
    ) -> tuple[Any, list[RuntimeEvent]]:
        if not self._codex_available or self._router is None:
            raise SubagentProviderError(
                "codex_subagent_unavailable",
                "coding task requires the Codex child provider, but it is unavailable",
            )
        request = SpawnSubagentRequest(
            provider_ref=DEFAULT_CODEX_CHILD_PROVIDER_REF,
            parent_session_id=parent_run.state.session_id,
            parent_run_id=parent_run.handle.run_id,
            task=task,
            policy=SubagentPolicy(
                max_depth=1,
                timeout_seconds=self._child_timeout_seconds,
                max_steps=self._child_max_turns,
            ),
            metadata={"call_id": call_id, "label": label, "task_kind": "coding"},
        )
        handle = await self._router.spawn(request)
        self._active.setdefault(parent_run.handle.run_id, {})[call_id] = handle
        try:
            result = await self._router.result(handle)
            child_events = [event async for event in self._router.subscribe(handle)]
            # Codex can emit hundreds of token/item deltas. The parent Studio
            # needs lifecycle progress, not a second copy of child text or its
            # private reasoning stream. Preserve only distinct public states;
            # the terminal output still returns through the delegate tool.
            projected: list[RuntimeEvent] = []
            seen_states: set[str] = set()
            for event in child_events:
                state = str(event.payload.get("state") or "")
                if event.kind not in {"progress", "terminal"} or not state or state in seen_states:
                    continue
                seen_states.add(state)
                projected.append(
                    self._progress_event(
                        parent_run,
                        kind="subagent.event",
                        status=state,
                        call_id=call_id,
                        label=label,
                        provider_ref=handle.provider_ref,
                        child_handle_id=handle.handle_id,
                        child_event_kind=event.kind,
                        child_seq=event.seq,
                    )
                )
            if result.state != "succeeded":
                raise SubagentProviderError(
                    result.error_code or "codex_subagent_failed",
                    result.error_message or f"Codex child ended as {result.state}",
                )
            return result.output, projected
        finally:
            self._active.get(parent_run.handle.run_id, {}).pop(call_id, None)
            await self._router.dispose(handle)

    async def cancel_parent(self, parent_run_id: str) -> None:
        if self._router is None:
            return
        handles = list(self._active.pop(parent_run_id, {}).values())
        for handle in handles:
            try:
                await self._router.cancel(handle)
            finally:
                await self._router.dispose(handle)

    def clear_run(self, parent_run_id: str) -> None:
        self._started.pop(parent_run_id, None)
        self._active.pop(parent_run_id, None)

    @classmethod
    def _publish_progress(cls, parent_run: Any, **payload: Any) -> None:
        """Publish child lifecycle immediately instead of after tool completion."""
        from ksadk.harness.subagent import resequence_child_events

        resequence_child_events(
            parent_run,
            [
                cls._progress_event(
                    parent_run,
                    kind="subagent.event",
                    **payload,
                )
            ],
            observe=True,
        )

    @staticmethod
    def public_label(arguments: dict[str, Any], *, call_id: str = "") -> str:
        """Return a short responsibility label without exposing the task body."""
        label = str(arguments.get("label") or "").strip()
        if not label:
            task = " ".join(str(arguments.get("task") or "").split())
            label = re.split(r"[。；;，,\n]", task, maxsplit=1)[0].strip()
        return (label or call_id or "处理子任务")[:32]

    @staticmethod
    def _child_name(prefix: str, call_id: str) -> str:
        safe = re.sub(r"[^a-z0-9_-]+", "-", call_id.lower()).strip("-")[:40]
        return f"{prefix}-{safe or 'child'}"

    @staticmethod
    def _progress_event(parent_run: Any, **payload: Any) -> RuntimeEvent:
        return RuntimeEvent.create(
            EventType.RUN_PROGRESS,
            agent_id=parent_run.state.agent_id,
            user_id=parent_run.state.user_id,
            session_id=parent_run.state.session_id,
            invocation_id=parent_run.handle.run_id,
            seq_id=0,
            payload=payload,
        )


__all__ = [
    "AdaptiveDelegationRuntime",
    "DELEGATE_TASK_TOOL",
    "DelegationDecision",
    "HARNESS_CHILD_PROVIDER_REF",
]
