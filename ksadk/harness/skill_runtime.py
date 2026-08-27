"""Harness Skill 渐进披露（plan §10.4）。

四级披露：Level 0 名称+一句话 → Level 1 Manifest → Level 2 完整 SKILL.md →
Level 3 引用资源仅执行时加载。Skill 管理面（包校验/安全解压/Loader）留在
现有 Skill 子系统，本模块只负责披露层级控制。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


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
        return self._source.resource(skill_id, resource_ref)


__all__ = ["SkillDisclosureError", "SkillManifest", "SkillRuntime"]
