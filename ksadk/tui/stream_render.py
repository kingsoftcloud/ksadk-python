"""TUI 流式渲染共享纯函数。

从 RemoteRunner.stream 的归一化 chunk 提取 delta/usage/终止信号，格式化 usage 文本。
被 loop.py（交互 TUI）和 cmd_invoke._invoke_once（-m 单次）共用，保证两路径渲染口径一致。
"""

from __future__ import annotations

import re
from typing import Any, Optional


def clean_response(text: str) -> str:
    """清理 LLM 响应中的内部调试伪影。

    只删内部调试残留（Tool Result 标记、python repr 片段、tool_call_id），
    不动正常的 XML/HTML 标签（用户可能需要 <tag> 输出）。
    """
    text = re.sub(r"\[Tool Result:.*?\]", "", text, flags=re.DOTALL)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"name='[^']*'\s*tool_call_id='[^']*'", "", text)
    return text.strip()


def extract_stream_delta(chunk: dict) -> tuple[str, dict | None, bool]:
    """从 RemoteRunner.stream 的 chunk 提取 (delta 文本, usage, 是否终止信号)。

    - text/thinking 等普通 chunk：取 delta，非终止。
    - final（chat 路径结束，output=完整文本）/ responses_output（responses 路径
      response.completed，output=list）：只取 usage，不把 output 当 delta——
      否则末尾会把完整文本或 list 再 append 一遍（重复/乱码）。
    """
    chunk_type = str(chunk.get("type") or "text")
    if chunk_type in {"final", "responses_output"}:
        usage = chunk.get("usage")
        usage = dict(usage) if isinstance(usage, dict) else None
        # terminal chunk 可能带 metadata.last_usage（上下文用量），合并进 usage
        # 让 TUI 能显示 context 占比，不丢这条 metadata。
        metadata = chunk.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("last_usage"), dict):
            if usage is None:
                usage = {}
            usage.setdefault("last_usage", dict(metadata["last_usage"]))
        return "", (usage or None), True
    delta = chunk.get("delta") or ""
    if not isinstance(delta, str):
        delta = str(delta or "")
    return delta, None, False


def format_usage(usage: Optional[dict[str, Any]]) -> str:
    """把 usage dict 格式成紧凑文本：↑输入 ↓输出 ⌀总计。

    兼容 input_tokens/output_tokens（chat 路径）与 prompt_tokens/completion_tokens
    （responses 路径）两种字段名。
    """
    if not isinstance(usage, dict) or not usage:
        return ""
    inp = usage.get("input_tokens") or usage.get("prompt_tokens")
    out = usage.get("output_tokens") or usage.get("completion_tokens")
    total = usage.get("total_tokens")
    parts = []
    if inp:
        parts.append(f"↑{inp}")
    if out:
        parts.append(f"↓{out}")
    if total:
        parts.append(f"⌀{total}")
    return " ".join(parts)
