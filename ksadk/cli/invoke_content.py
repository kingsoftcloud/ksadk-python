"""Extract display text from supported remote response payloads."""

from __future__ import annotations


def _extract_content(chunk: dict) -> tuple[str, str]:
    """从 OpenAI 流式响应中提取内容 (包含 reasoning_content)"""
    event_name = str(chunk.get("_event") or "")
    if event_name == "response.output_text.delta":
        return str(chunk.get("delta") or ""), ""
    # reasoning delta 有 4 个变体名(与 remote_runner / 前端 chatProtocol 对齐),
    # 主 runtime 发 reasoning.delta,Studio shared_web 发 reasoning_summary_text.delta。
    if event_name in {
        "response.reasoning.delta",
        "response.reasoning_text.delta",
        "response.reasoning_summary.delta",
        "response.reasoning_summary_text.delta",
    }:
        return "", str(chunk.get("delta") or chunk.get("text") or "")
    if event_name == "response.completed":
        return "", ""

    # OpenAI 格式: {"choices": [{"delta": {"content": "xxx", "reasoning_content": "thought"}}]}
    try:
        choices = chunk.get("choices", [])
        if choices:
            delta = choices[0].get("delta", {})
            return delta.get("content", "") or "", delta.get("reasoning_content", "") or ""
    except (KeyError, IndexError):
        pass

    if isinstance(chunk.get("output_text"), str):
        return chunk["output_text"], ""
    if isinstance(chunk.get("delta"), str):
        return chunk["delta"], ""
    return "", ""


def _extract_response_content(response: dict) -> str:
    """从 OpenAI 非流式响应中提取内容"""
    output_text = response.get("output_text")
    if output_text:
        return str(output_text)
    output = response.get("output") or []
    for item in output:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("text"):
                return str(content["text"])

    # OpenAI 格式: {"choices": [{"message": {"content": "xxx"}}]}
    try:
        choices = response.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
            return str(message.get("content") or "")
    except (KeyError, IndexError):
        pass
    return str(response)
