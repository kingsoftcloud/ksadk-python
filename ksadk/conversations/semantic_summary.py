from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import httpx

from ksadk.configs.settings import settings
from ksadk.conversations.compaction_prompt import (
    build_compaction_prompt_messages,
    extract_summary_text,
)
from ksadk.conversations.context import (
    canonical_event_type,
    extract_event_text,
    summarize_event_groups,
)
from ksadk.sessions.base import SessionEvent

SUMMARY_VERSION = "v1"
DEFAULT_COMPACTION_SUMMARY_TIMEOUT_MS = 45_000
DEFAULT_COMPACTION_SUMMARY_MAX_GROUPS = 12
SUMMARY_PREFIX = "Earlier conversation summary:"

# L4 semantic 熔断:独立计数 semantic LLM 调用失败(不复用 governance compact failure counter,
# 因为 summarize_compaction 捕获异常后返回 extractive,外层看是成功,governance 不触发)。
# 超过阈值后直接走 extractive,避免 Claude Code 的"1279 会话连续失败 50+ 次"浪费。
# 已知局限(有意取舍,对齐 Claude Code 行为):
# - 默认 0(opt-in),避免 transient 失败永久禁用 semantic;用户显式设 N>0 才启用。
# - 打开后无 half-open 探测,需进程重启或显式 _reset_semantic_failures 恢复(CC 同样如此)。
# - 计数为模块级全局,跨 session/model 共享。
# - per-model key 化留作 follow-up(对齐 governance per-session 范式)。
_semantic_summary_failures: int = 0


def _max_consecutive_semantic_failures() -> int:
    raw = os.environ.get("KSADK_MAX_CONSECUTIVE_SEMANTIC_FAILURES", "0")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _semantic_circuit_open() -> bool:
    """semantic 熔断是否打开(超阈值则跳过 LLM 直接走 extractive)。0 = 禁用熔断。"""
    threshold = _max_consecutive_semantic_failures()
    if threshold <= 0:
        return False
    return _semantic_summary_failures >= threshold


def _record_semantic_failure() -> None:
    global _semantic_summary_failures
    _semantic_summary_failures += 1


def _reset_semantic_failures() -> None:
    global _semantic_summary_failures
    _semantic_summary_failures = 0


@dataclass
class CompactionSummaryResult:
    """一次 checkpoint 摘要的标准结果。"""

    summary_text: str
    summary_strategy: str
    summary_version: str = SUMMARY_VERSION
    summary_model: str = ""
    summary_usage: dict[str, Any] = field(default_factory=dict)
    fallback_reason: str | None = None


# --- PR D2：Session Working State（方案 §9.3） ---


@dataclass
class WorkingState:
    """压缩前后保持任务连续性的结构化工作面，随 ContextCheckpoint 持久化。

    生成原则（方案 §9.3）：优先确定性提取——``pending_tools``/``pending_approvals``/receipt
    从事实事件取；``current_goal`` 从最新 user_message/pinned_state 取；``active_files`` 从
    workspace 工具调用参数取。仅 ``decisions``/``errors_and_corrections``/``next_action`` 等
    难结构化项允许从摘要文本解析（带 fallback）。不跨 Session 召回，不写 MemoryProvider。
    """

    current_goal: str = ""
    current_phase: str | None = None
    completed_steps: list[str] = field(default_factory=list)
    pending_steps: list[str] = field(default_factory=list)
    next_action: str | None = None
    active_files: list[dict[str, object]] = field(default_factory=list)
    decisions: list[dict[str, object]] = field(default_factory=list)
    errors_and_corrections: list[dict[str, object]] = field(default_factory=list)
    pending_tools: list[dict[str, object]] = field(default_factory=list)
    pending_approvals: list[dict[str, object]] = field(default_factory=list)
    artifact_refs: list[dict[str, object]] = field(default_factory=list)
    # §8.1：关键约束（"不得操作生产环境" 等），从摘要/pinned_state 提取，缺失时合并旧值。
    constraints: list[str] = field(default_factory=list)
    source_seq_range: tuple[int, int] = (0, 0)
    schema_version: str = "v1"

    def critical_fields_present(self) -> bool:
        """§8.1：关键字段校验。四项必须全部非空（P0 严格验收，不可放宽）。

        - current_goal 非空
        - constraints 非空
        - completed_steps 非空
        - next_action 非空
        """
        return (
            bool(self.current_goal and self.current_goal.strip())
            and len(self.constraints) > 0
            and len(self.completed_steps) > 0
            and bool(self.next_action and self.next_action.strip())
        )

    def merge_missing_from(self, previous: "WorkingState | None") -> "WorkingState":
        """§8.1：关键字段缺失时用压缩前 WorkingState 合并，不接受空值覆盖。

        current_goal/constraints/completed_steps/next_action 空时回填 previous 的值
        （避免压缩后丢失"不得操作生产环境"等关键约束和已完成进展）。pending_tools/approvals
        始终以事实事件提取为准（不合并，防过期 pending）。
        """
        if previous is None:
            return self
        if not self.current_goal.strip():
            self.current_goal = previous.current_goal
        if not self.constraints:
            self.constraints = list(previous.constraints)
        if not self.next_action and previous.next_action:
            self.next_action = previous.next_action
        if not self.completed_steps and previous.completed_steps:
            self.completed_steps = list(previous.completed_steps)
        return self

    def to_audit_dict(self) -> dict[str, Any]:
        """审计用 plain dict（写 checkpoint metadata）。不含 prompt 明文，只含结构化字段。"""
        return {
            "current_goal": self.current_goal,
            "next_action": self.next_action,
            "completed_steps": list(self.completed_steps),
            "completed_steps_count": len(self.completed_steps),
            "pending_steps": list(self.pending_steps),
            "pending_steps_count": len(self.pending_steps),
            "active_files": list(self.active_files),
            "decisions_count": len(self.decisions),
            "errors_and_corrections_count": len(self.errors_and_corrections),
            "pending_tools": list(self.pending_tools),
            "pending_approvals": list(self.pending_approvals),
            "artifact_refs": list(self.artifact_refs),
            "constraints": list(self.constraints),
            "source_seq_range": list(self.source_seq_range),
            "schema_version": self.schema_version,
            "content_hash": self.content_hash(),
            "status": "succeeded",
        }

    def content_hash(self) -> str:
        import hashlib

        payload = json.dumps(
            {
                "current_goal": self.current_goal,
                "next_action": self.next_action,
                "completed_steps": self.completed_steps,
                "pending_steps": self.pending_steps,
                "active_files": self.active_files,
                "decisions": self.decisions,
                "errors_and_corrections": self.errors_and_corrections,
                "pending_tools": self.pending_tools,
                "pending_approvals": self.pending_approvals,
                "artifact_refs": self.artifact_refs,
                "constraints": self.constraints,
                "source_seq_range": list(self.source_seq_range),
                "schema_version": self.schema_version,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _parse_summary_v2_sections(
    summary_text: str,
) -> tuple[
    str | None,
    list[dict[str, object]],
    list[dict[str, object]],
    str,
    list[str],
    list[str],
]:
    """从摘要 v2 结构化文本确定性解析 next_action / decisions / errors_and_corrections /
    current_goal / constraints（方案 §9.4 / P0 Working State 验收）。

    支持中文标记（"当前用户目标"/"关键约束"/"下一步工作位置"）与英文标记。纯文本解析，无 LLM。
    容错：无标记时返回 ``(None, [], [], "", [])``。
    """
    text = str(summary_text or "").strip()
    if not text:
        return None, [], [], "", [], []

    def _find_section(*labels: str) -> str:
        for label in labels:
            # 形如 "下一步工作位置：<内容>"，到下一个已知标记或末尾
            for marker in (f"{label}：", f"{label}:", f"{label} "):
                idx = text.find(marker)
                if idx >= 0:
                    body = text[idx + len(marker) :].strip()
                    # 截到下一个已知 section 标记
                    stop = len(body)
                    for other in (
                        "下一步",
                        "下一步工作",
                        "未完成事项",
                        "重要决策",
                        "错误修正",
                        "当前用户目标",
                        "关键约束",
                        "已完成进展",
                        "重要引用",
                        "Next",
                        "Next Step",
                        "Decision",
                        "Error",
                        "Pending",
                        "最新用户指令",
                    ):
                        if other.startswith(label):
                            continue
                        pos = body.find(other)
                        if pos >= 0 and pos < stop:
                            stop = pos
                    return body[:stop].strip().strip("。.；;")
        return ""

    next_action = _find_section("下一步工作位置", "下一步", "Next Step", "Next") or None
    decisions_text = _find_section("重要决策", "关键决策", "Decision")
    errors_text = _find_section("错误修正", "错误与纠正", "Error")
    decisions = [{"text": decisions_text}] if decisions_text else []
    errors_and_corrections = [{"text": errors_text}] if errors_text else []
    # P0：current_goal / constraints / completed_steps 从摘要解析（方案 §9.3/§9.4）
    current_goal = _find_section("当前用户目标", "当前目标", "Current Goal", "Goal") or ""
    constraints_text = _find_section("关键约束", "重要约束", "Constraints", "Constraint")
    constraints = (
        [c.strip() for c in constraints_text.split("；;") if c.strip()] if constraints_text else []
    )
    completed_text = _find_section("已完成进展", "已完成", "Completed", "Progress")
    completed_steps = (
        [s.strip() for s in completed_text.split("；;") if s.strip()] if completed_text else []
    )
    return (
        next_action,
        decisions,
        errors_and_corrections,
        current_goal,
        constraints,
        completed_steps,
    )


def extract_working_state(
    events: Sequence[SessionEvent],
    *,
    pinned_state: Mapping[str, Any] | None = None,
    summary_text: str = "",
    source_seq_range: tuple[int, int] = (0, 0),
) -> WorkingState:
    """从事实事件确定性提取 WorkingState（方案 §9.3）。

    ``pending_tools``/``pending_approvals``/``current_goal``/``active_files`` 来自事件，
    不靠摘要模型猜测（与 ``extract_pinned_state`` 同源但结构化）。``decisions``/
    ``errors_and_corrections``/``next_action`` 暂留空（v2 摘要文本解析留 follow-up，
    当前优先确定性事实）。容错：v1 旧摘要或缺失字段时返回部分填充。
    """
    pinned = dict(pinned_state or {})
    # pending_tools / pending_approvals：复用 pinned_state 的确定性提取结果（已去配对）。
    pending_tools_raw = list(pinned.get("pending_tools") or [])
    pending_approvals_raw = list(pinned.get("pending_approvals") or [])
    artifact_refs_raw = list(pinned.get("attachment_refs") or [])
    current_goal = str(pinned.get("current_user_goal") or "").strip()
    # constraints 从 pinned_state 取（确定性），缺失时由摘要解析补充
    constraints_raw = list(pinned.get("constraints") or [])
    # completed_steps 从 pinned_state 取（确定性），缺失时由摘要解析补充
    completed_steps_raw = list(pinned.get("completed_steps") or [])

    # active_files：从 tool_call 事件的 tool_args.path 提取（workspace 类工具）。
    active_files: list[dict[str, object]] = []
    seen_paths: set[str] = set()
    for event in events:
        event_type = canonical_event_type(
            event.event_type,
            author=event.author,
            role=str((event.content or {}).get("role") or ""),
        )
        if event_type != "tool_call":
            continue
        meta = event.metadata or {}
        tool_args = meta.get("tool_args")
        if isinstance(tool_args, Mapping):
            path = str(tool_args.get("path") or tool_args.get("file") or "").strip()
            if path and path not in seen_paths:
                seen_paths.add(path)
                active_files.append({"path": path, "tool_name": str(meta.get("tool_name") or "")})

    # 摘要 v2 文本解析（方案 §9.3 / §9.4 / P0）：从结构化摘要确定性解析 next_action / decisions /
    # errors_and_corrections / current_goal / constraints / completed_steps。
    (
        next_action,
        decisions,
        errors_and_corrections,
        summary_goal,
        summary_constraints,
        summary_completed,
    ) = _parse_summary_v2_sections(summary_text)
    # current_goal 优先用 pinned_state，缺失时用摘要解析的 goal
    if not current_goal.strip() and summary_goal:
        current_goal = summary_goal
    # constraints 优先用 pinned_state/事件，缺失时用摘要解析
    if not constraints_raw and summary_constraints:
        constraints_raw = list(summary_constraints)
    # completed_steps 优先用 pinned_state，缺失时用摘要解析
    completed_steps = (
        list(completed_steps_raw)
        if completed_steps_raw
        else (list(summary_completed) if summary_completed else [])
    )

    return WorkingState(
        current_goal=current_goal,
        next_action=next_action,
        decisions=decisions,
        errors_and_corrections=errors_and_corrections,
        completed_steps=completed_steps,
        active_files=active_files[-10:],
        pending_tools=[{"text": t} for t in pending_tools_raw],
        pending_approvals=[{"text": t} for t in pending_approvals_raw],
        artifact_refs=[{"ref": r} for r in artifact_refs_raw],
        constraints=list(constraints_raw),
        source_seq_range=source_seq_range,
    )


class SummaryModelClient:
    """独立的摘要模型客户端。

    注意这里故意不复用 agent runner：
    - runner 的职责是执行用户 agent；
    - 摘要器的职责是平台内部的 context compaction。
    两者拆开后，失败隔离和后续替换模型都会更清晰。
    """

    def __init__(
        self,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
    ):
        self.api_base = str(api_base or settings.model.api_base or "").rstrip("/")
        self.api_key = str(api_key or settings.model.api_key or "").strip()

    @property
    def is_available(self) -> bool:
        return bool(self.api_base and self.api_key)

    def _chat_completions_url(self) -> str:
        if self.api_base.endswith("/v1"):
            return f"{self.api_base}/chat/completions"
        return f"{self.api_base}/v1/chat/completions"

    async def summarize(
        self,
        *,
        model: str,
        messages: Sequence[dict[str, str]],
        timeout_ms: int,
    ) -> tuple[str, dict[str, Any]]:
        if not self.is_available:
            raise RuntimeError("summary model client is not configured")
        if not model:
            raise RuntimeError("summary model is not configured")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "model": model,
            "messages": list(messages),
            "stream": False,
            "temperature": 0,
        }
        timeout_seconds = max(1.0, float(timeout_ms) / 1000.0)
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                self._chat_completions_url(), headers=headers, json=payload
            )
            response.raise_for_status()
            data = response.json()

        choices = data.get("choices") or []
        message = choices[0].get("message") if choices else {}
        content = message.get("content") if isinstance(message, Mapping) else ""
        if isinstance(content, list):
            fragments: list[str] = []
            for item in content:
                if isinstance(item, Mapping) and item.get("text"):
                    fragments.append(str(item["text"]))
            content = "\n".join(fragment for fragment in fragments if fragment)
        text = str(content or "").strip()
        if not text:
            raise RuntimeError("summary model returned empty content")
        return text, dict(data.get("usage") or {})


def resolve_summary_model_client() -> SummaryModelClient:
    return SummaryModelClient()


def _ensure_summary_prefix(summary_text: str) -> str:
    text = str(summary_text or "").strip()
    if not text or text.startswith(SUMMARY_PREFIX):
        return text
    return f"{SUMMARY_PREFIX}\n{text}"


def _env_truthy(name: str) -> bool:
    value = str(os.getenv(name, "")).strip().lower()
    return value in {"1", "true", "yes", "on"}


def semantic_compaction_disabled() -> bool:
    return _env_truthy("COMPACTION_DISABLE_SEMANTIC")


def get_summary_timeout_ms() -> int:
    raw = os.getenv("COMPACTION_SUMMARY_TIMEOUT_MS", "").strip()
    try:
        return max(1_000, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_COMPACTION_SUMMARY_TIMEOUT_MS


def get_summary_max_groups() -> int:
    raw = os.getenv("COMPACTION_SUMMARY_MAX_GROUPS", "").strip()
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_COMPACTION_SUMMARY_MAX_GROUPS


def resolve_summary_model(current_model: str | None) -> str:
    override = str(os.getenv("COMPACTION_SUMMARY_MODEL", "")).strip()
    if override:
        return override
    if current_model:
        return str(current_model)
    return str(settings.model.model_name or "")


def find_pinned_group_indexes(groups: Sequence[Sequence[SessionEvent]]) -> set[int]:
    """找出不能被 compact 的轮次。

    当前先严格保护两类未完成状态：
    1. approval_request 尚未收到 approval_response
    2. tool_call 尚未看到 tool_result
    """

    pending_approval_groups: list[int] = []
    pending_tool_groups: list[int] = []
    for index, group in enumerate(groups):
        for event in group:
            event_type = canonical_event_type(
                event.event_type,
                author=event.author,
                role=str((event.content or {}).get("role") or ""),
            )
            if event_type == "approval_request":
                pending_approval_groups.append(index)
                continue
            if event_type == "approval_response" and pending_approval_groups:
                pending_approval_groups.pop()
                continue
            if event_type == "tool_call":
                pending_tool_groups.append(index)
                continue
            if event_type == "tool_result" and pending_tool_groups:
                pending_tool_groups.pop()
    return set(pending_approval_groups + pending_tool_groups)


def extract_pinned_state(groups: Sequence[Sequence[SessionEvent]]) -> dict[str, Any]:
    """提取必须在 checkpoint 里显式保留的状态。

    P0：除了 pending approvals/tools/attachment_refs/current_user_goal，还确定性提取
    constraints（"不得操作生产环境" 等）和 completed_steps（"镜像已构建" 等），
    从 user/assistant 消息文本中按标记提取，不依赖摘要模型。
    """

    pending_approvals: list[str] = []
    pending_tools: list[str] = []
    attachment_refs: list[str] = []
    current_user_goal = ""
    constraints: list[str] = []
    completed_steps: list[str] = []

    # 约束标记：用户说"不得/不要/禁止X"或 assistant 说"约束：X"
    import re

    constraint_patterns = [
        re.compile(r"(?:不得|不要|禁止|不能|严禁)[^。\n；;]{2,50}"),
    ]
    # 完成标记：assistant 说"X已构建/X完成/X构建完成"
    # 捕获完整短语（含"已构建"等），不拆开
    completed_patterns = [
        re.compile(
            r"([\u4e00-\u9fa5A-Za-z0-9 ]{2,30}"
            r"(?:已构建|已完成|已成功|构建完成|构建好了"
            r"|做完了|搞定了|改完了|修好了|测完了|跑通了|部署完成|配置完成))"
        ),
    ]

    for group in groups:
        for event in group:
            event_type = canonical_event_type(
                event.event_type,
                author=event.author,
                role=str((event.content or {}).get("role") or ""),
            )
            text = extract_event_text(event)
            if event_type == "approval_request" and text:
                pending_approvals.append(text)
            elif event_type == "approval_response" and pending_approvals:
                pending_approvals.pop()
            elif event_type == "tool_call" and text:
                pending_tools.append(text)
            elif event_type == "tool_result" and pending_tools:
                pending_tools.pop()
            elif event_type == "attachment_ref":
                attachment_refs.append(text)
            elif event_type == "user_message" and text:
                current_user_goal = text
                for attachment in (event.metadata or {}).get("attachments") or []:
                    if not isinstance(attachment, Mapping):
                        continue
                    label = str(
                        attachment.get("display_name")
                        or attachment.get("file_uri")
                        or attachment.get("storage_path")
                        or ""
                    ).strip()
                    if label:
                        attachment_refs.append(label)
                # P0：从 user 消息提取约束
                for pattern in constraint_patterns:
                    for m in pattern.findall(text):
                        c = m.strip().rstrip("，。；;")
                        if c and c not in constraints:
                            constraints.append(c)
            elif event_type == "assistant_message" and text:
                # P0：从 assistant 消息提取已完成步骤
                for pattern in completed_patterns:
                    for m in pattern.findall(text):
                        s = m.strip().rstrip("，。；;")
                        if s and s not in completed_steps:
                            completed_steps.append(s)

    unique_attachments: list[str] = []
    for item in attachment_refs:
        normalized = str(item or "").strip()
        if normalized and normalized not in unique_attachments:
            unique_attachments.append(normalized)

    return {
        "pending_approvals": pending_approvals,
        "pending_tools": pending_tools,
        "attachment_refs": unique_attachments[-5:],
        "current_user_goal": current_user_goal,
        "constraints": constraints,
        "completed_steps": completed_steps,
    }


def _build_semantic_input(
    *,
    previous_summary: str,
    groups_to_compact: Sequence[Sequence[SessionEvent]],
    pinned_state: Mapping[str, Any],
    model_metadata: Mapping[str, Any] | None,
) -> tuple[str, list[list[SessionEvent]]]:
    max_groups = get_summary_max_groups()
    selected_groups = [list(group) for group in groups_to_compact]
    merged_previous_summary = previous_summary.strip()
    if len(selected_groups) > max_groups:
        skipped_groups = selected_groups[:-max_groups]
        selected_groups = selected_groups[-max_groups:]
        skipped_summary = summarize_event_groups(
            skipped_groups, previous_summary=merged_previous_summary
        )
        merged_previous_summary = skipped_summary
    return merged_previous_summary, selected_groups


async def summarize_compaction(
    *,
    groups_to_compact: Sequence[Sequence[SessionEvent]],
    previous_summary: str,
    pinned_state: Mapping[str, Any],
    model_metadata: Mapping[str, Any] | None,
    model: str | None,
) -> CompactionSummaryResult:
    """语义摘要优先，失败后自动回退到 extractive。"""

    fallback_summary = summarize_event_groups(
        [list(group) for group in groups_to_compact],
        previous_summary=previous_summary,
    )
    if semantic_compaction_disabled():
        return CompactionSummaryResult(
            summary_text=fallback_summary,
            summary_strategy="extractive",
            fallback_reason="semantic summarizer disabled",
        )

    # L4 semantic 熔断:连续失败超阈值则跳过 LLM 直接走 extractive,直到进程重启或显式 reset。
    if _semantic_circuit_open():
        return CompactionSummaryResult(
            summary_text=fallback_summary,
            summary_strategy="extractive",
            fallback_reason="semantic_circuit_open",
        )

    summary_model = resolve_summary_model(model)
    client = resolve_summary_model_client()
    if not client.is_available:
        return CompactionSummaryResult(
            summary_text=fallback_summary,
            summary_strategy="extractive",
            fallback_reason="summary model client is not configured",
        )

    try:
        merged_previous_summary, selected_groups = _build_semantic_input(
            previous_summary=previous_summary,
            groups_to_compact=groups_to_compact,
            pinned_state=pinned_state,
            model_metadata=model_metadata,
        )
        prompt_messages = build_compaction_prompt_messages(
            previous_summary=merged_previous_summary,
            groups_to_compact=selected_groups,
            pinned_state=pinned_state,
            model_metadata=model_metadata,
        )
        raw_text, usage = await client.summarize(
            model=summary_model,
            messages=prompt_messages,
            timeout_ms=get_summary_timeout_ms(),
        )
        summary_text = extract_summary_text(raw_text)
        if not summary_text:
            raise RuntimeError("summary model returned empty <summary> block")
        # LLM 调用成功,清零 semantic 失败计数。
        _reset_semantic_failures()
        return CompactionSummaryResult(
            summary_text=_ensure_summary_prefix(summary_text),
            summary_strategy="semantic",
            summary_model=summary_model,
            summary_usage=usage,
        )
    except Exception as exc:
        # LLM 调用失败(非 extractive 主动选择),记录 semantic 失败用于熔断判定。
        _record_semantic_failure()
        return CompactionSummaryResult(
            summary_text=fallback_summary,
            summary_strategy="extractive",
            fallback_reason=str(exc) or exc.__class__.__name__,
        )
