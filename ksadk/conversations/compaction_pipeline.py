"""分层 compaction pipeline(L2 Snip + L3 Microcompact + L5 working set)。

接入 ``compact_conversation_history``(runtime.py),在送 LLM summarizer 前做零成本确定性裁剪。

核心纪律(Codex review):
- transcript 是 append-only,绝不删事件。L2/L3 只作用于"送 summarizer 的 candidate 投影":
  浅拷贝 groups + 过滤/合成,返回新的 candidate groups。原始 SessionEvent 实例和 transcript 不动。
- L3 不伪造 transcript 事件:合成的摘要 event 只存在于 candidate 投影里,不写回 transcript。
- checkpoint metadata 记录 pipeline 执行情况(snip_stats/microcompact_stats/preserved_receipts),
  便于审计和恢复。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ksadk.conversations.context import (
    canonical_event_type,
    extract_event_text,
    summarize_event_groups,
)
from ksadk.conversations.model_context import estimate_text_tokens
from ksadk.sessions.base import SessionEvent


@dataclass
class SnipStats:
    """L2 Snip 执行统计,写入 checkpoint metadata 便于审计。"""

    removed_redundant_tool_results: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    covered_seq_range: tuple[int, int] | None = None


@dataclass
class SnipResult:
    groups: list[list[SessionEvent]]
    stats: SnipStats


@dataclass
class MicrocompactStats:
    """L3 Microcompact 执行统计。"""

    compacted_groups: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    preserved_receipts: list[str] = field(default_factory=list)


@dataclass
class MicrocompactResult:
    groups: list[list[SessionEvent]]
    stats: MicrocompactStats


def _snip_enabled() -> bool:
    return _bool_env("KSADK_COMPACT_SNIP_ENABLED", True)


def _microcompact_enabled() -> bool:
    return _bool_env("KSADK_COMPACT_MICROCOMPACT_ENABLED", True)


def _microcompact_cold_rounds() -> int:
    """超过多少轮未引用的组算冷组,默认 3。"""
    raw = os.environ.get("KSADK_COMPACT_MICROCOMPACT_COLD_ROUNDS", "3")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 3


def _bool_env(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def candidate_tokens(groups: Sequence[Sequence[SessionEvent]]) -> int:
    """估算 candidate groups 的总 token(用于 pipeline 渐进停止判断)。"""
    return sum(
        estimate_text_tokens(extract_event_text(event)) for group in groups for event in group
    )


def _tool_signature(event: SessionEvent) -> str | None:
    """识别 tool_call 事件的 tool 名 + 参数 hash,用于判断是否被后续组覆盖。

    兼容真实 runtime event 形态(tool 名在 metadata.tool_name,参数在 metadata.tool_args)
    和历史/合成形态(tool 名在 content.name,参数在 content.arguments)。
    """
    event_type = canonical_event_type(
        event.event_type,
        author=event.author,
        role=str((event.content or {}).get("role") or ""),
    )
    if event_type != "tool_call":
        return None
    content = event.content or {}
    metadata = event.metadata or {}
    name = str(metadata.get("tool_name") or content.get("name") or content.get("tool_name") or "")
    # 参数优先取 metadata.tool_args(真实 runtime),回退 content.arguments(合成/历史)。
    arguments = (
        metadata.get("tool_args")
        or content.get("arguments")
        or content.get("input")
        or content.get("args")
        or ""
    )
    if not isinstance(arguments, str):
        try:
            arguments_hash_input = repr(arguments)
        except Exception:
            arguments_hash_input = ""
    else:
        arguments_hash_input = arguments
    if not name:
        return None
    digest = hashlib.sha1(f"{name}:{arguments_hash_input}".encode("utf-8")).hexdigest()[:12]
    return f"{name}:{digest}"


def _event_run_id(event: SessionEvent) -> str | None:
    """取 event 的 run_id(真实 runtime 把 run_id 放 metadata,tool_call/tool_result 都有)。

    用于精确配对 tool_call 和 tool_result:同一工具调用的 call 和 result 共享同一 run_id。
    比"邻接配对"更可靠,支持 parallel_tool_calls(同一轮多个 tool_call 交错 result)。
    """
    metadata = event.metadata or {}
    run_id = metadata.get("run_id")
    if run_id:
        return str(run_id)
    # 兼容 tool_receipt 里也可能带 run_id/tool_call_id 的形态。
    receipt = metadata.get("tool_receipt")
    if isinstance(receipt, Mapping):
        for key in ("run_id", "tool_call_id"):
            value = receipt.get(key)
            if value:
                return str(value)
    return None


def snip_redundant_groups(
    groups: Sequence[Sequence[SessionEvent]],
    *,
    pinned_state: Mapping[str, Any] | None = None,
) -> SnipResult:
    """L2 Snip:在 candidate 投影上移除冗余 tool_result。

    只作用于 candidate 副本(浅拷贝 groups + 过滤 event),不改原 SessionEvent,不改 transcript。

    移除规则:
    - 被**后续组**同 tool_name 同参数覆盖的旧 tool_call(含其配对 tool_result)——
      旧结果已无参考价值,模型只需看最新结果。但**最后一组的 tool 保留**(tail 保护)。
    - tool_call 与 tool_result 按 **metadata.run_id 精确配对**删除(不靠邻接),
      支持 parallel_tool_calls=True 时同一轮多 tool_call 交错的场景。

    失败 tool_call/tool_result 配对的去重本期不做(保守起见,避免误删有诊断价值的失败记录)。
    pinned_state 里的 pending tool/approval 不动(它们在 pinned 组,不在 groups_to_compact)。
    """
    tokens_before = candidate_tokens(groups)
    if not _snip_enabled() or not groups:
        return SnipResult(
            groups=[list(group) for group in groups],
            stats=SnipStats(tokens_before=tokens_before, tokens_after=tokens_before),
        )

    # 第一遍:记录每个 tool_signature 最后一次出现的组索引,以及每个 run_id 对应的 signature。
    # run_id 来自 tool_call 的 metadata,用于精确配对 tool_result。
    last_signature_group: dict[str, int] = {}
    run_id_to_signature: dict[str, str] = {}
    for index, group in enumerate(groups):
        for event in group:
            sig = _tool_signature(event)
            if sig is not None:
                last_signature_group[sig] = index
                run_id = _event_run_id(event)
                if run_id:
                    run_id_to_signature[run_id] = sig

    # 被覆盖的 tool_call:其 signature 最后一次出现不在当前组。
    # 收集这些被覆盖 tool_call 的 run_id,用于精确删除配对 tool_result(不靠邻接)。
    covered_run_ids: set[str] = set()
    for index, group in enumerate(groups):
        for event in group:
            sig = _tool_signature(event)
            if sig is not None and last_signature_group.get(sig) != index:
                run_id = _event_run_id(event)
                if run_id:
                    covered_run_ids.add(run_id)

    removed_redundant = 0
    result_groups: list[list[SessionEvent]] = []
    for index, group in enumerate(groups):
        kept: list[SessionEvent] = []
        for event in group:
            sig = _tool_signature(event)
            if sig is not None:
                # 被后续组覆盖的旧 tool_call 移除(保留最后一次出现)。
                if last_signature_group.get(sig) != index:
                    removed_redundant += 1
                    continue
            else:
                event_type = canonical_event_type(
                    event.event_type,
                    author=event.author,
                    role=str((event.content or {}).get("role") or ""),
                )
                if event_type == "tool_result":
                    run_id = _event_run_id(event)
                    # 配对的 tool_call 已被覆盖删除,按 run_id 精确删除该 result(避免孤儿)。
                    if run_id and run_id in covered_run_ids:
                        removed_redundant += 1
                        continue
            kept.append(event)
        result_groups.append(kept)

    tokens_after = candidate_tokens(result_groups)
    head_seq = groups[0][0].seq_id if groups and groups[0] else 0
    tail_seq = groups[-1][-1].seq_id if groups and groups[-1] else 0
    return SnipResult(
        groups=result_groups,
        stats=SnipStats(
            removed_redundant_tool_results=removed_redundant,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            covered_seq_range=(head_seq, tail_seq) if groups else None,
        ),
    )


def microcompact_cold_groups(
    groups: Sequence[Sequence[SessionEvent]],
    *,
    pinned_state: Mapping[str, Any] | None = None,
    previous_summary: str = "",
) -> MicrocompactResult:
    """L3 Microcompact:对冷组做 extractive 摘要,整组替换成合成摘要 event(candidate 投影)。

    只作用于 candidate 副本,不写回 transcript。合成 event 只存在于送 summarizer 的 candidate 里。
    保留 receipt_key/framework_ref(从 pinned_state 和 event metadata 提取,记入 stats)。
    """
    tokens_before = candidate_tokens(groups)
    if not _microcompact_enabled() or len(groups) <= _microcompact_cold_rounds():
        return MicrocompactResult(
            groups=[list(group) for group in groups],
            stats=MicrocompactStats(tokens_before=tokens_before, tokens_after=tokens_before),
        )

    # 尾部 N 组保留(它们可能还被模型需要),更早的组视为冷组。
    cold_rounds = _microcompact_cold_rounds()
    cold_groups = (
        [list(group) for group in groups[:-cold_rounds]] if cold_rounds < len(groups) else []
    )
    tail_groups = (
        [list(group) for group in groups[-cold_rounds:]]
        if cold_rounds < len(groups)
        else [list(group) for group in groups]
    )

    if not cold_groups:
        return MicrocompactResult(
            groups=[list(group) for group in groups],
            stats=MicrocompactStats(tokens_before=tokens_before, tokens_after=tokens_before),
        )

    # 用现有 extractive summarizer 生成结构化摘要文本(不调 LLM)。
    summary_text = summarize_event_groups(cold_groups, previous_summary=previous_summary)

    # 收集冷组里的 receipt_key/framework_ref,记入 stats(preserved_receipts)。
    preserved_receipts: list[str] = []
    for group in cold_groups:
        for event in group:
            metadata = event.metadata or {}
            receipt = str(metadata.get("receipt_key") or "").strip()
            if receipt and receipt not in preserved_receipts:
                preserved_receipts.append(receipt)

    # 合成一个摘要 event 放入 candidate 头部(不写回 transcript)。
    # event_type 用 assistant_message 避免下游误判为真实 tool 事件。
    summary_event = SessionEvent(
        event_type="assistant_message",
        author="system",
        content={
            "role": "assistant",
            "text": f"[earlier rounds microcompact summary]\n{summary_text}",
        },
        metadata={
            "compaction_projection": True,
            "compaction_stage": "microcompact",
            "preserved_receipts": preserved_receipts,
        },
        seq_id=cold_groups[0][0].seq_id if cold_groups and cold_groups[0] else 0,
    )

    result_groups = [[summary_event], *tail_groups]
    tokens_after = candidate_tokens(result_groups)
    return MicrocompactResult(
        groups=result_groups,
        stats=MicrocompactStats(
            compacted_groups=len(cold_groups),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            preserved_receipts=preserved_receipts,
        ),
    )


def build_working_set_metadata(
    *,
    pinned_state: Mapping[str, Any] | None,
    recent_files: Sequence[Mapping[str, Any]] | None = None,
    active_tools: Sequence[str] | None = None,
) -> dict[str, Any]:
    """L5 working set 恢复(保守版):只记 metadata,不读文件内容。

    recent_files: 调用方从 workspace_state 取最近 N 个文件的
        {path, mtime_ns, size_bytes, read_range}。
    active_tools: 调用方从 describe_agentengine_tools 取 enabled 工具名。
    下一轮 prompt 注入时只提示"这些文件最近读过",不注入内容(避免 token 膨胀)。
    """
    return {
        "recent_files": list(recent_files or [])[: _working_set_max_files()],
        "active_tools": list(active_tools or []),
        "pinned_approvals": list((pinned_state or {}).get("pending_approvals") or []),
        "pinned_tools": list((pinned_state or {}).get("pending_tools") or []),
        "current_user_goal": str((pinned_state or {}).get("current_user_goal") or ""),
    }


def _working_set_max_files() -> int:
    raw = os.environ.get("KSADK_WORKING_SET_MAX_FILES", "5")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 5


def run_pipeline(
    groups: Sequence[Sequence[SessionEvent]],
    *,
    threshold_tokens: int,
    pinned_state: Mapping[str, Any] | None = None,
    previous_summary: str = "",
) -> dict[str, Any]:
    """编排 L2 → L3,渐进停止。返回 candidate groups + 各层 stats。

    L4(LLM compact)由调用方(summarize_compaction)继续处理 candidate。
    L5(working set)由调用方在写 checkpoint 时调 build_working_set_metadata。
    """
    snip = snip_redundant_groups(groups, pinned_state=pinned_state)
    candidate = snip.groups
    snip_released = snip.stats.tokens_before - snip.stats.tokens_after

    micro: MicrocompactResult | None = None
    if candidate_tokens(candidate) > threshold_tokens:
        micro = microcompact_cold_groups(
            candidate,
            pinned_state=pinned_state,
            previous_summary=previous_summary,
        )
        candidate = micro.groups

    return {
        "candidate_groups": candidate,
        "snip_stats": snip.stats,
        "microcompact_stats": micro.stats if micro else None,
        "tokens_before": snip.stats.tokens_before,
        "tokens_after": candidate_tokens(candidate),
        "snip_released_tokens": snip_released,
        "pipeline_stages": [
            stage
            for stage, used in (
                ("snip", True),
                ("microcompact", micro is not None),
            )
            if used
        ],
    }
