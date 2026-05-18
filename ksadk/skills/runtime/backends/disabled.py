from __future__ import annotations

from ksadk.skills.runtime.base import SkillRuntimeError, SkillRuntimeResult


class DisabledSkillRuntimeBackend:
    def run_workflow(self, *args, **kwargs) -> SkillRuntimeResult:
        raise SkillRuntimeError("Skill Runtime is disabled")
