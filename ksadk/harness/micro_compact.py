"""G27 上下文减压分层（旧工具结果清壳，保留审计壳）（总案 §22 G27）。

只做"清发送正文 + 保留审计壳 + 边界记账"，**不新增第二套压缩路径**：
扫描历史消息，除最后 N 条 tool result 外，其余 tool result 的发送正文
替换为占位壳，transcript 原文保留（审计不丢）。这是发送侧清理，不产生
CompactionRecord，与 §25.4 约束1（保持一条阶梯）一致——清壳是一档减压，
不是第二条摘要链。

接入点：prepare_context 装配后、proactive 触发前调用，作为 proactive 之前
的一道减压，推迟全量压缩触发。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _count_tokens(text: str) -> int:
    """粗略 token 估算（字符数 / 4）；与 count_tokens 同量级即可用于记账。"""
    return max(1, len(text) // 4)


@dataclass
class MicroCompactor:
    """旧工具结果清壳器（发送侧减压，保留审计壳）。

    ``keep_last_n_tool_results``：保留最近 N 条 tool result 原文，其余清壳。
    """

    keep_last_n_tool_results: int = 3

    def micro_compact(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """扫描历史，对超出 keep_last_n 的 tool result 清壳。

        返回 (发送用 messages 副本, meta)；transcript 原文不动（调用方保留）。
        meta: ``compacted_tool_ids``（被清壳的 tool_call_id）、``tokens_saved``。
        """
        tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        # 需要清壳的：除最后 N 条 tool result 之外
        to_shell = set(tool_indices[: max(0, len(tool_indices) - self.keep_last_n_tool_results)])
        out: list[dict[str, Any]] = []
        compacted_ids: list[str] = []
        tokens_saved = 0
        for i, m in enumerate(messages):
            if i in to_shell:
                original = str(m.get("content") or "")
                original_tokens = _count_tokens(original)
                tool_name = m.get("name") or "tool"
                shell = f"[已归档 tool:{tool_name} 结果，原文在 transcript 审计壳中]"
                shell_tokens = _count_tokens(shell)
                tokens_saved += max(0, original_tokens - shell_tokens)
                compacted_ids.append(str(m.get("tool_call_id") or ""))
                new_m = dict(m)
                new_m["content"] = shell
                out.append(new_m)
            else:
                out.append(dict(m))
        meta = {
            "compacted_tool_ids": compacted_ids,
            "tokens_saved": tokens_saved,
            "shelled_count": len(compacted_ids),
        }
        return out, meta


__all__ = ["MicroCompactor"]
