from __future__ import annotations

from typing import Any


_FENCE_MARKERS = ("```", "~~~")
_LIST_PREFIXES = ("- ", "* ", "+ ")


def repair_markdown(text: Any, *, enabled: bool = False) -> str:
    """尽量修复 LLM 输出中的常见 Markdown 形态问题。

    这是业务侧按需调用的保守修复工具。KsADK runtime 默认保留模型原文，
    不会自动调用这个函数。
    """

    if text is None:
        return ""
    value = str(text)
    if not enabled:
        return value
    if value == "":
        return ""

    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = _strip_terminal_empty_lines(normalized.split("\n"))
    if not lines:
        return ""

    lines = _normalize_block_spacing(lines)
    lines = _close_unclosed_fence(lines)

    return "\n".join(lines) + "\n"


def _strip_terminal_empty_lines(lines: list[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and lines[start] == "":
        start += 1
    while end > start and lines[end - 1] == "":
        end -= 1
    return lines[start:end]


def _normalize_block_spacing(lines: list[str]) -> list[str]:
    result: list[str] = []
    in_fence = False
    fence_marker = ""
    previous_block = ""

    for index, line in enumerate(lines):
        stripped = line.lstrip()
        block = _line_block_type(line, in_fence)

        if block == "fence":
            marker = _fence_marker(stripped) or ""
            if not in_fence:
                _append_blank_before_block(result, previous_block)
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
        elif not in_fence:
            if block in {"table", "list"}:
                _append_blank_before_block(result, previous_block)
            elif block == "paragraph":
                _append_blank_after_block(result, previous_block)

        result.append(line)

        if line == "":
            previous_block = ""
        elif not in_fence or block == "fence":
            previous_block = block

        next_line = lines[index + 1] if index + 1 < len(lines) else None
        if next_line is None or in_fence:
            continue
        next_block = _line_block_type(next_line, in_fence)
        if block in {"table", "list"} and next_block == "paragraph":
            _append_blank_once(result)
            previous_block = ""

    return result


def _append_blank_before_block(result: list[str], previous_block: str) -> None:
    if not result or result[-1] == "":
        return
    if previous_block in {"", "table", "list", "fence"}:
        return
    result.append("")


def _append_blank_after_block(result: list[str], previous_block: str) -> None:
    if result and result[-1] != "" and previous_block in {"list", "table", "fence"}:
        result.append("")


def _append_blank_once(result: list[str]) -> None:
    if result and result[-1] != "":
        result.append("")


def _close_unclosed_fence(lines: list[str]) -> list[str]:
    open_marker = ""
    for line in lines:
        marker = _fence_marker(line.lstrip())
        if marker is None:
            continue
        if not open_marker:
            open_marker = marker
        elif marker == open_marker:
            open_marker = ""
    if open_marker:
        return [*lines, open_marker]
    return lines


def _line_block_type(line: str, in_fence: bool) -> str:
    if line == "":
        return ""
    stripped = line.lstrip()
    if _fence_marker(stripped):
        return "fence"
    if in_fence:
        return "code"
    if _is_table_line(stripped):
        return "table"
    if _is_list_line(stripped):
        return "list"
    return "paragraph"


def _fence_marker(stripped_line: str) -> str | None:
    for marker in _FENCE_MARKERS:
        if stripped_line.startswith(marker):
            return marker
    return None


def _is_table_line(stripped_line: str) -> bool:
    return stripped_line.startswith("|") and stripped_line.endswith("|") and stripped_line.count("|") >= 2


def _is_list_line(stripped_line: str) -> bool:
    if stripped_line.startswith(_LIST_PREFIXES):
        return True
    dot_index = stripped_line.find(". ")
    if dot_index <= 0:
        return False
    return stripped_line[:dot_index].isdigit()
