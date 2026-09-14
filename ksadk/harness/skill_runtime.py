"""Harness Skill 渐进披露（plan §10.4）。

四级披露：Level 0 名称+一句话 → Level 1 Manifest → Level 2 完整 SKILL.md →
Level 3 引用资源仅执行时加载。Skill 管理面（包校验/安全解压/Loader）留在
现有 Skill 子系统，本模块只负责披露层级控制。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ksadk.skills.loader import load_local_skill

SKILL_MANIFEST_TOOL = "skill_read_manifest"
SKILL_INSTRUCTIONS_TOOL = "skill_read_instructions"
SKILL_RESOURCE_TOOL = "skill_read_resource"


class SkillDisclosureError(RuntimeError):
    """越级披露（未先读过 Level 1/2 就要求 Level 3 正文/资源）。"""


@dataclass(frozen=True)
class SkillManifest:
    """Level 1：Manifest + 使用条件 + 权限需求。"""

    name: str
    summary: str
    conditions: str = ""
    required_tools: tuple[str, ...] = ()


class SkillSource(Protocol):
    """Skill 内容源（宿主注入，对接现有 Skill Loader）。"""

    def manifest(self, skill_id: str) -> SkillManifest: ...

    def full_text(self, skill_id: str) -> str: ...

    def resource(self, skill_id: str, resource_ref: str) -> bytes: ...


@dataclass(frozen=True)
class SkillDisclosureTool:
    """暴露给模型的只读披露工具描述；执行仍由默认 Agent Loop 接管。"""

    name: str
    description: str
    parameters: dict[str, Any]
    source: str = "harness:skill-disclosure"

    @property
    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class LocalSkillSource:
    """把已校验、已解压的本地 Skill 目录适配为披露内容源。

    ``skill_roots`` 的键应是 Revision 中固定版本的完整 Skill 引用。包下载、
    校验和安全解压仍由 ``ksadk.skills`` 负责，本类不承担管理面职责。
    """

    def __init__(self, skill_roots: dict[str, str | Path]) -> None:
        self._roots = {skill_id: Path(root).resolve() for skill_id, root in skill_roots.items()}

    def _root(self, skill_id: str) -> Path:
        try:
            return self._roots[skill_id]
        except KeyError:
            raise KeyError(f"skill {skill_id!r} 未绑定本地内容目录") from None

    def manifest(self, skill_id: str) -> SkillManifest:
        skill = load_local_skill(self._root(skill_id))
        return SkillManifest(name=skill.name, summary=skill.description or skill.name)

    def full_text(self, skill_id: str) -> str:
        return load_local_skill(self._root(skill_id)).body

    def resource(self, skill_id: str, resource_ref: str) -> bytes:
        root = self._root(skill_id)
        candidate = (root / resource_ref).resolve()
        if candidate == root or not candidate.is_relative_to(root):
            raise SkillDisclosureError(f"skill {skill_id} 资源路径越界: {resource_ref!r}")
        if not candidate.is_file():
            raise FileNotFoundError(f"skill {skill_id} 资源不存在: {resource_ref!r}")
        return candidate.read_bytes()


class SkillRuntime:
    """渐进披露控制器：记录每个 (run, skill) 已到达的最高层级。"""

    def __init__(self, source: SkillSource) -> None:
        self._source = source
        self._levels: dict[tuple[str, str], int] = {}

    def level(self, run_id: str, skill_id: str) -> int:
        return self._levels.get((run_id, skill_id), 0)

    # ------------------------------------------------------------- 披露

    def level0(self, skill_id: str) -> str:
        """名称 + 一句话描述（进入上下文的最小信息）。"""
        return self._source.manifest(skill_id).summary

    def catalog_entry(self, skill_id: str) -> dict[str, str]:
        """Level 0 的结构化目录项；读取目录不提升披露级别。"""
        manifest = self._source.manifest(skill_id)
        return {"skill_id": skill_id, "name": manifest.name, "summary": manifest.summary}

    def recommend(
        self,
        skill_ids: tuple[str, ...],
        *,
        query: str,
        limit: int = 5,
    ) -> tuple[dict[str, str], ...]:
        """Rank bound Skills without loading L1/L2/L3 content.

        This is deliberately deterministic and metadata-only: it improves discovery
        while keeping model execution, installation and version selection outside of
        the recommender.  A zero-score catalog keeps Revision order.
        """
        entries = [self.catalog_entry(skill_id) for skill_id in skill_ids]
        query_terms = _search_terms(query)
        ranked: list[tuple[float, int, dict[str, str]]] = []
        for index, entry in enumerate(entries):
            name_terms = _search_terms(entry["name"])
            summary_terms = _search_terms(entry["summary"])
            name_hits = len(query_terms.intersection(name_terms))
            summary_hits = len(query_terms.intersection(summary_terms))
            score = float(name_hits * 3 + summary_hits)
            ranked.append((score, index, entry))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        positive = {id(entry) for score, _index, entry in ranked[:limit] if score > 0}
        return tuple(
            {
                **entry,
                "recommended": "true" if id(entry) in positive else "false",
                "recommendation_score": f"{score:g}",
            }
            for score, _index, entry in ranked
        )

    def level1(self, run_id: str, skill_id: str) -> SkillManifest:
        manifest = self._source.manifest(skill_id)
        self._levels[(run_id, skill_id)] = max(self.level(run_id, skill_id), 1)
        return manifest

    def level2(self, run_id: str, skill_id: str) -> str:
        """完整 SKILL.md：必须先读过 Level 1（Manifest）。"""
        if self.level(run_id, skill_id) < 1:
            raise SkillDisclosureError(f"skill {skill_id} 须先披露 Level 1 (Manifest) 再读正文")
        self._levels[(run_id, skill_id)] = 2
        return self._source.full_text(skill_id)

    def level3(self, run_id: str, skill_id: str, resource_ref: str) -> bytes:
        """引用资源：仅执行时加载，必须已披露 Level 2。"""
        if self.level(run_id, skill_id) < 2:
            raise SkillDisclosureError(f"skill {skill_id} 须先披露 Level 2 (SKILL.md) 再加载资源")
        instructions = self._source.full_text(skill_id)
        if resource_ref not in instructions:
            raise SkillDisclosureError(f"skill {skill_id} 的 SKILL.md 未引用资源 {resource_ref!r}")
        resource = self._source.resource(skill_id, resource_ref)
        self._levels[(run_id, skill_id)] = 3
        return resource

    def clear_run(self, run_id: str) -> None:
        """Run 关闭后清理披露游标，避免长期进程累积状态。"""
        for key in [key for key in self._levels if key[0] == run_id]:
            self._levels.pop(key, None)

    @staticmethod
    def disclosure_tools() -> tuple[SkillDisclosureTool, ...]:
        skill_id = {
            "type": "string",
            "description": "Revision 已绑定的完整 Skill 引用。",
        }
        return (
            SkillDisclosureTool(
                name=SKILL_MANIFEST_TOOL,
                description=(
                    "读取已绑定 Skill 的 Manifest、适用条件和所需工具。"
                    "使用 Skill 前必须先调用此工具。"
                ),
                parameters={
                    "type": "object",
                    "properties": {"skill_id": skill_id},
                    "required": ["skill_id"],
                    "additionalProperties": False,
                },
            ),
            SkillDisclosureTool(
                name=SKILL_INSTRUCTIONS_TOOL,
                description=("读取 Skill 的完整操作说明。必须先读取同一 Skill 的 Manifest。"),
                parameters={
                    "type": "object",
                    "properties": {"skill_id": skill_id},
                    "required": ["skill_id"],
                    "additionalProperties": False,
                },
            ),
            SkillDisclosureTool(
                name=SKILL_RESOURCE_TOOL,
                description=("按引用读取 Skill 附属资源。必须先读取同一 Skill 的完整操作说明。"),
                parameters={
                    "type": "object",
                    "properties": {
                        "skill_id": skill_id,
                        "resource_ref": {
                            "type": "string",
                            "description": "SKILL.md 中引用的相对资源路径。",
                        },
                    },
                    "required": ["skill_id", "resource_ref"],
                    "additionalProperties": False,
                },
            ),
        )

    @staticmethod
    def content_digest(content: str | bytes) -> str:
        raw = content.encode("utf-8") if isinstance(content, str) else content
        return "sha256:" + hashlib.sha256(raw).hexdigest()


def _search_terms(value: str) -> set[str]:
    """Produce small English tokens and CJK bi-grams for metadata matching."""
    normalized = value.casefold()
    terms = set(re.findall(r"[a-z0-9][a-z0-9._-]*", normalized))
    for chunk in re.findall(r"[\u3400-\u9fff]+", normalized):
        if len(chunk) == 1:
            terms.add(chunk)
        else:
            terms.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return terms


__all__ = [
    "LocalSkillSource",
    "SKILL_INSTRUCTIONS_TOOL",
    "SKILL_MANIFEST_TOOL",
    "SKILL_RESOURCE_TOOL",
    "SkillDisclosureError",
    "SkillDisclosureTool",
    "SkillManifest",
    "SkillRuntime",
]
