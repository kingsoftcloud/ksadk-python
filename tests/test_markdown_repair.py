from __future__ import annotations

from ksadk.markdown import repair_markdown


def test_repair_markdown_closes_unclosed_fenced_code_block():
    raw = "下面是示例：\n```python\nprint('hello')"

    repaired = repair_markdown(raw, enabled=True)

    assert repaired == "下面是示例：\n\n```python\nprint('hello')\n```\n"


def test_repair_markdown_preserves_already_closed_fenced_code_block():
    raw = "说明\n\n```python\nprint('hello')\n```\n"

    repaired = repair_markdown(raw, enabled=True)

    assert repaired == raw


def test_repair_markdown_normalizes_blank_lines_around_tables_and_lists():
    raw = "结果如下：\n| 名称 | 值 |\n| --- | --- |\n| A | 1 |\n结论：\n- 第一项\n- 第二项\n下一段"

    repaired = repair_markdown(raw, enabled=True)

    assert repaired == (
        "结果如下：\n\n"
        "| 名称 | 值 |\n"
        "| --- | --- |\n"
        "| A | 1 |\n\n"
        "结论：\n\n"
        "- 第一项\n"
        "- 第二项\n\n"
        "下一段\n"
    )


def test_repair_markdown_is_disabled_by_default():
    raw = "结果如下：\n| 名称 | 值 |\n| --- | --- |\n| A | 1 |\n结论：\n- 第一项\n下一段"

    repaired = repair_markdown(raw)

    assert repaired == raw


def test_repair_markdown_can_be_enabled_with_one_switch():
    raw = "结果如下：\n| 名称 | 值 |\n| --- | --- |\n| A | 1 |\n结论：\n- 第一项\n下一段"

    repaired = repair_markdown(raw, enabled=True)

    assert repaired == (
        "结果如下：\n\n"
        "| 名称 | 值 |\n"
        "| --- | --- |\n"
        "| A | 1 |\n\n"
        "结论：\n\n"
        "- 第一项\n\n"
        "下一段\n"
    )


def test_repair_markdown_is_idempotent():
    raw = "标题\n```json\n{\"ok\": true}"

    once = repair_markdown(raw, enabled=True)
    twice = repair_markdown(once, enabled=True)

    assert once == twice


def test_repair_markdown_handles_empty_and_non_string_values():
    assert repair_markdown("") == ""
    assert repair_markdown(None) == ""
    assert repair_markdown(123) == "123"
    assert repair_markdown(123, enabled=True) == "123\n"
