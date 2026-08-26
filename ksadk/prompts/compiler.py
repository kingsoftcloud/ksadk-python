"""PromptCompiler —— 确定性编译 Prompt 分区（方案第 7 节）。

第一个 PR 只落地稳定数据模型；本（第二个）PR 实现 ``compile()`` 的确定性行为：排序、
标准化、merge policy、protected 覆盖检测、SHA-256、section token 与 stable_prefix_hash。

PR2 仍是 shadow：``CompiledPrompt`` 仅用于 hash/可观测/未来 projection，**不替换** Runner
实际发送的 instructions→new_message/SystemMessage/base_instructions 拼装，线上行为不变。
``CompiledPrompt.content`` 不保证等于 Runner 最终物理输入（方案 7.2）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Iterable

from ksadk.context_engine.tokenizer import get_default_token_counter
from ksadk.prompts.models import (
    PROMPT_COMPILER_VERSION,
    CompiledPrompt,
    PromptSection,
    PromptSectionKind,
)

# canonical 顺序：按 priority 升序，priority 相同按 kind 字典序。同一 section_id 的
# source/stability/merge_policy 必须固定，不因 Runner 遍历顺序变化（方案 7.3 第 8 条）。
_KIND_ORDER: tuple[PromptSectionKind, ...] = (
    "platform_safety",
    "agent_identity",
    "agent_policy",
    "runtime_capabilities",
    "resource_manifest",
    "request_instructions",
)


def _section_sort_key(section: PromptSection) -> tuple[int, str, str]:
    kind_rank = _KIND_ORDER.index(section.kind) if section.kind in _KIND_ORDER else len(_KIND_ORDER)
    kind_key = _KIND_ORDER[kind_rank] if kind_rank < len(_KIND_ORDER) else section.kind
    return (section.priority, kind_key, section.section_id)


def _normalize_text(text: str) -> str:
    """标准化换行与尾部空白，但不改变正文语义（方案 7.3 第 2 条）。

    - CRLF/CR → LF
    - 去除每行尾部空白
    - 合并 3+ 连续空行为 1 行，去除首尾空白
    """
    if not text:
        return ""
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n")]
    # 合并 2+ 连续空行 → 单空行（统一段落间距，不改变正文语义）
    collapsed: list[str] = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
            if blank_run >= 2:
                continue
            collapsed.append(line)
        else:
            blank_run = 0
            collapsed.append(line)
    return "\n".join(collapsed).strip()


def _content_sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _wrap_section(section: PromptSection, content: str) -> str:
    """用标签消除区段歧义，但不应把所有动态上下文拼成 XML（方案 7.3 推荐格式）。"""
    body = content.strip()
    if not body:
        return ""
    return f"<{section.kind}>\n{body}\n</{section.kind}>"


def _merge_sections(sections: list[PromptSection]) -> list[PromptSection]:
    """同 kind 多来源时执行显式 merge policy，禁止"最后写入悄悄覆盖"（方案 7.3 第 3 条）。

    - ``replace``: 保留最后一条（同 section_id 之内）
    - ``append``: 按顺序拼接
    - ``merge_unique``: 去重拼接
    - ``protected``: 不允许覆盖；发生覆盖尝试时抛 ``ProtectedSectionOverrideError``
    """
    grouped: dict[str, list[PromptSection]] = {}
    order: list[str] = []
    for section in sections:
        if section.section_id not in grouped:
            grouped[section.section_id] = []
            order.append(section.section_id)
        grouped[section.section_id].append(section)

    merged: list[PromptSection] = []
    for section_id in order:
        bucket = grouped[section_id]
        head = bucket[0]
        if len(bucket) == 1:
            merged.append(head)
            continue
        policy = head.merge_policy
        # 同一 section_id 的来源/稳定性/merge_policy 必须固定（方案 7.3 第 8 条）。
        inconsistent = any(
            b.merge_policy != policy or b.stability != head.stability or b.kind != head.kind
            for b in bucket[1:]
        )
        if inconsistent:
            raise InconsistentSectionError(
                f"section {section_id!r} 的来源/稳定性/merge_policy 不一致"
            )
        if policy == "replace":
            merged.append(replace(head, content=bucket[-1].content))
        elif policy == "protected":
            # 任意两条同 section_id 内容不同即视为覆盖尝试。
            contents = {b.content for b in bucket}
            if len(contents) > 1:
                raise ProtectedSectionOverrideError(
                    f"protected section {section_id!r} 发生覆盖尝试"
                )
            merged.append(head)
        elif policy == "merge_unique":
            seen: set[str] = set()
            parts: list[str] = []
            for b in bucket:
                for chunk in re.split(r"\n{2,}", b.content.strip()):
                    chunk = chunk.strip()
                    if chunk and chunk not in seen:
                        seen.add(chunk)
                        parts.append(chunk)
            merged.append(replace(head, content="\n\n".join(parts)))
        else:  # append
            merged.append(
                replace(
                    head,
                    content="\n\n".join(b.content.strip() for b in bucket if b.content.strip()),
                )
            )
    return merged


class ProtectedSectionOverrideError(RuntimeError):
    """``protected`` section 被尝试覆盖。编译失败并产生审计事件，不静默忽略（方案 7.3 第 9 条）。"""


class InconsistentSectionError(RuntimeError):
    """同一 section_id 的来源/稳定性/merge_policy 不固定。"""


@dataclass(frozen=True)
class PromptCompiler:
    """确定性 Prompt 编译器。

    ``compile()`` 必须对相同输入产生相同输出（方案 7.3）。构造器无状态，可复用。
    """

    compiler_version: str = PROMPT_COMPILER_VERSION

    def compile(self, sections: Iterable[PromptSection]) -> CompiledPrompt:
        # 1. 排序（确定性）
        ordered = sorted(sections, key=_section_sort_key)
        # 2. merge（同 section_id 的多来源按 merge policy 合并）
        merged = _merge_sections(ordered)
        # 3. platform_safety 不允许被 request_instructions 覆盖（方案 7.3 第 4 条）。
        #    protected/replace 在 _merge_sections 内已处理；这里额外校验跨 section_id 的
        #    信任边界：request_instructions 不得声明 platform 的 kind。
        for section in merged:
            if (
                section.kind == "platform_safety"
                and section.trust_level in ("untrusted", "user")
            ):
                raise ProtectedSectionOverrideError(
                    "platform_safety 不得由 untrusted/user 来源声明"
                )

        counter = get_default_token_counter()
        # 4. 空 section 不输出占位文本（方案 7.3 第 5 条）。
        section_hashes: dict[str, str] = {}
        tokens_by_section: dict[str, int] = {}
        wrapped_blocks: list[str] = []
        for section in merged:
            body = _normalize_text(section.content)
            section_hashes[section.section_id] = _content_sha256(body)
            tokens_by_section[section.section_id] = counter.count_text(body)
            if body:
                wrapped_blocks.append(_wrap_section(section, body))
        canonical_content = "\n\n".join(block for block in wrapped_blocks if block).strip()

        # 5. stable_prefix_hash：仅覆盖 stability="stable" 的 section（platform_safety /
        #    agent_identity / agent_policy）。部署级/动态 section 不进稳定前缀（方案 7.4）。
        stable_body = "\n\n".join(
            _wrap_section(s, _normalize_text(s.content))
            for s in merged
            if s.stability == "stable" and _normalize_text(s.content)
        ).strip()
        stable_prefix_hash = _content_sha256(stable_body) if stable_body else ""

        return CompiledPrompt(
            sections=tuple(merged),
            content=canonical_content,
            content_hash=_content_sha256(canonical_content),
            estimated_tokens=counter.count_text(canonical_content),
            stable_prefix_hash=stable_prefix_hash,
            section_hashes=section_hashes,
            tokens_by_section=tokens_by_section,
            compiler_version=self.compiler_version,
        )


def compile_prompt(sections: Iterable[PromptSection]) -> CompiledPrompt:
    """便捷入口：用默认 compiler 编译。"""
    return PromptCompiler().compile(sections)
