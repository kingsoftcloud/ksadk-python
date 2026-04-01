from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from ksadk.conversations.context import canonical_event_type, extract_event_text
from ksadk.sessions.base import SessionEvent

_NO_TOOLS_PROMPT = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- Do NOT use any tool or function call.
- You already have all required context in this request.
- Your entire response must be plain text with exactly two blocks:
  <analysis>...</analysis>
  <summary>...</summary>
- The <summary> block must use Chinese and contain exactly these sections:
  当前用户目标
  关键约束与偏好
  已完成进展
  重要决策/代码上下文
  未完成事项
  下一步工作位置
"""

_SUMMARY_USER_PROMPT = """你正在为一个 append-only 会话日志生成压缩摘要。

目标：
1. 帮助系统在压缩较早对话后，仍然能稳定恢复当前工作上下文。
2. 明确保留用户目标、约束、已完成进展、关键代码上下文、未完成事项。
3. 不要编造不存在的事实；不确定时明确写“未明确”。

输出要求：
- 先输出 <analysis>，仅作为草稿。
- 再输出 <summary>。
- <summary> 内必须只包含以下固定标题，且顺序不能变：
  当前用户目标
  关键约束与偏好
  已完成进展
  重要决策/代码上下文
  未完成事项
  下一步工作位置

模型上下文信息：
{model_metadata}

上一个 checkpoint 摘要：
{previous_summary}

需要强制保真的 pinned state：
{pinned_state}

本次要压缩的旧轮次：
{groups_text}
"""


def _truncate_text(text: str, *, limit: int = 400) -> str:
    stripped = str(text or "").strip()
    if len(stripped) <= limit:
        return stripped
    return f"{stripped[:limit]}...(已截断)"


def _format_group(group: Sequence[SessionEvent], *, index: int) -> str:
    lines = [f"## Round {index}"]
    for event in group:
        event_type = canonical_event_type(
            event.event_type,
            author=event.author,
            role=str((event.content or {}).get("role") or ""),
        )
        text = _truncate_text(extract_event_text(event), limit=320)
        if not text:
            continue
        lines.append(
            f"- seq={event.seq_id} type={event_type} author={event.author or 'unknown'} text={text}"
        )
    return "\n".join(lines)


def build_compaction_prompt_messages(
    *,
    previous_summary: str,
    groups_to_compact: Sequence[Sequence[SessionEvent]],
    pinned_state: Mapping[str, Any],
    model_metadata: Mapping[str, Any] | None,
) -> list[dict[str, str]]:
    """构造 semantic compaction 的固定 prompt。

    这里单独抽成 builder，是为了让本地和云端后续都能稳定复用同一套摘要结构，
    不把 prompt 字符串散落到 runtime 编排里。
    """

    groups_text = "\n\n".join(
        _format_group(group, index=index + 1)
        for index, group in enumerate(groups_to_compact)
    ).strip() or "无可压缩内容"
    user_prompt = _SUMMARY_USER_PROMPT.format(
        model_metadata=json.dumps(dict(model_metadata or {}), ensure_ascii=False, indent=2),
        previous_summary=previous_summary.strip() or "无",
        pinned_state=json.dumps(dict(pinned_state or {}), ensure_ascii=False, indent=2),
        groups_text=groups_text,
    )
    return [
        {"role": "system", "content": _NO_TOOLS_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


def extract_summary_text(response_text: str) -> str:
    """提取 <summary>，并丢弃 <analysis> 草稿。"""

    raw = str(response_text or "").strip()
    if not raw:
        return ""

    summary_match = re.search(r"<summary>\s*(.*?)\s*</summary>", raw, flags=re.DOTALL | re.IGNORECASE)
    if summary_match:
        return summary_match.group(1).strip()

    without_analysis = re.sub(
        r"<analysis>.*?</analysis>",
        "",
        raw,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return without_analysis.strip()
