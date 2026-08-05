from __future__ import annotations

import os
from pathlib import Path

import pytest

from ksadk.prompts.sources import (
    DEFAULT_RULE_FILE_MAX_TOKENS,
    DEFAULT_RULE_FILES_MAX_TOKENS,
    PLATFORM_SAFETY_TEXT,
    discover_instruction_files,
    platform_safety_section,
    request_instructions_section,
    sections_from_instructions,
)


def test_platform_safety_section_is_protected_and_stable() -> None:
    section = platform_safety_section()
    assert section.kind == "platform_safety"
    assert section.stability == "stable"
    assert section.merge_policy == "protected"
    assert section.overridable is False
    assert section.trust_level == "platform"
    assert section.content == PLATFORM_SAFETY_TEXT


def test_request_instructions_section_is_volatile() -> None:
    section = request_instructions_section("本次指令")
    assert section.kind == "request_instructions"
    assert section.stability == "volatile"
    assert section.priority == 60


def test_sections_from_instructions_empty_returns_empty() -> None:
    assert sections_from_instructions("") == []
    assert sections_from_instructions(None) == []


def test_discover_disabled_by_default(tmp_path: Path) -> None:
    # 默认 flag 关闭 → 不发现任何文件。
    (tmp_path / "AGENTS.md").write_text("# rules", encoding="utf-8")
    monkeypatch_env = {k: v for k, v in os.environ.items() if k != "KSADK_PROMPT_AUTO_DISCOVERY"}
    with pytest.MonkeyPatch.context() as mp:
        for key in list(os.environ):
            if key == "KSADK_PROMPT_AUTO_DISCOVERY":
                mp.delenv(key, raising=False)
        assert discover_instruction_files(tmp_path) == []


def test_discovery_parent_to_child_dedup_and_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KSADK_PROMPT_AUTO_DISCOVERY", "true")
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / "AGENTS.md").write_text("父级通用规则", encoding="utf-8")
    (child / "AGENTS.md").write_text("子级具体规则", encoding="utf-8")
    sections = discover_instruction_files(child)
    # 父级先进入，子级后进入（方案 7.6 第 3 条）。
    # 注意：按目录段定位，避免 tmp_path 目录名（含 "parent"/"child" 字样）干扰。
    sources = [s.source for s in sections]
    parent_idx = next(
        i for i, src in enumerate(sources) if src == str(parent / "AGENTS.md")
    )
    child_idx = next(
        i for i, src in enumerate(sources) if src == str(child / "AGENTS.md")
    )
    assert parent_idx < child_idx
    # 真实路径去重：同一文件不重复。
    assert len({s.metadata.get("path") for s in sections}) == len(sections)


def test_discovery_single_file_truncation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KSADK_PROMPT_AUTO_DISCOVERY", "true")
    # 构造一个明显超过单文件预算的文件。
    big = "规则行\n" * 2000
    (tmp_path / "AGENTS.md").write_text(big, encoding="utf-8")
    sections = discover_instruction_files(tmp_path, file_max_tokens=100)
    assert sections
    assert sections[0].metadata["truncated"] is True
    assert sections[0].metadata["tokens"] <= 100


def test_discovery_total_budget_caps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KSADK_PROMPT_AUTO_DISCOVERY", "true")
    (tmp_path / "AGENTS.md").write_text("规则A" * 500, encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("规则B" * 500, encoding="utf-8")
    sections = discover_instruction_files(tmp_path, file_max_tokens=DEFAULT_RULE_FILE_MAX_TOKENS, total_max_tokens=50)
    total = sum(s.metadata["tokens"] for s in sections)
    assert total <= 50
