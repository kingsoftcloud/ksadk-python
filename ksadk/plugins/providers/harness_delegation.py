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

import re
from dataclasses import dataclass
from typing import Any, Literal

from ksadk.harness.events import EventType, RuntimeEvent
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
    r"(?:\b(?:implement|refactor|debug|fix|patch|commit|modify|edit)\b|"
    r"\bwrite\s+(?:the\s+)?(?:code|tests?|implementation)\b|"
    r"\badd\s+(?:unit\s+|integration\s+)?tests?\b|"
    r"\bcoding\s+task\b|"
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
    ) -> None:
        if not 1 <= max_children <= 32:
            raise ValueError("dynamic delegation max_children must be in 1..32")
        self._router = router
        self._codex_available = codex_available
        self._max_children = max_children
        self._child_timeout_seconds = child_timeout_seconds
        self._child_max_turns = child_max_turns
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
                    "refactoring or tests; use general for research and analysis. Multiple "
                    "calls in one turn may "
                    "run in parallel. After successful delegation, synthesize the child outputs; "
                    "do not repeat the same research with parent tools unless a child explicitly "
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
            )
        return (
            {
                "provider": "codex" if decision.task_kind == "coding" else "harness",
                "provider_ref": decision.provider_ref,
                "task_kind": decision.task_kind,
                "label": label,
                "output": result,
            },
            events,
        )

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
        sub = SubAgentSpec(
            name=child_name,
            description=label,
            instructions=(
                "你是由 KsADK Harness 动态调度的通用子 Agent。只完成收到的独立子任务，"
                "使用可用证据，明确未验证内容，并把可供父 Agent 汇总的结论返回。"
                "调研任务应优先使用官方一手资料；当六条以内的高质量证据已足以回答时，"
                "立即停止继续搜索并形成结论。不得为了穷尽资料重复检索，最后一轮必须"
                "直接返回当前最佳结论以及仍未验证的事项。"
            ),
            tools=tuple(name for name in parent_run.tools if name != self.name),
            timeout_seconds=float(self._child_timeout_seconds),
            max_turns=self._child_max_turns,
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
