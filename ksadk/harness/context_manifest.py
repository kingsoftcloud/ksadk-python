"""ContextManifest（长任务 Context 与 Memory 增强方案 §6.2）。

ContextManifest 是 ContextPlan 的**可观测、可审计快照**：

- 规划层选择（ContextItem/ContextPlan）与投影层（AssembledInput）已存在；
- Manifest 只持久化 Hash、引用、选择原因和 Token 构成——**不重复持久化
  敏感正文**（方案 §6.2 / §12）；
- Planned / Projected / Actual 三段 Token 在这里闭环：Planned/Projected 在
  构建 Manifest 时填入，Actual 由引擎在模型返回 usage 后回填
  （``actual_usage_ref`` 指向 usage.reported 事件）。

设计为纯数据 + 纯函数模块（与 PCM 分层一致），引擎负责接线与持久化。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


def _manifest_id(material: str) -> str:
    return f"ctxm_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:12]}"


@dataclass(frozen=True)
class ManifestSection:
    """Manifest 中的单个 Section 条目：只记事实，不记正文。"""

    item_id: str
    kind: str
    source_refs: tuple[str, ...] = ()
    selected_reason: str = ""
    estimated_tokens: int = 0
    included: bool = True
    #: 内容 Hash（不含正文），用于比对两次调用间该段是否变化。
    content_hash: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "kind": self.kind,
            "source_refs": list(self.source_refs),
            "selected_reason": self.selected_reason,
            "estimated_tokens": self.estimated_tokens,
            "included": self.included,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class ContextManifest:
    """一次模型调用的 Context 快照（可持久化、可审计）。"""

    manifest_id: str
    run_id: str
    scope_id: str
    model_profile_ref: str
    stable_prompt_hash: str
    sections: tuple[ManifestSection, ...]
    context_window_tokens: int
    reserved_output_tokens: int
    available_input_tokens: int
    planned_tokens: int
    projected_tokens: int
    #: Actual Token 由引擎在 usage.reported 后回填（输入侧）。
    actual_input_tokens: int | None = None
    actual_output_tokens: int | None = None
    #: 指向 usage.reported 事件的 event_id。
    actual_usage_ref: str | None = None
    #: 触发本次重新构建的上一 manifest（如紧急压缩后重建）。
    supersedes: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "run_id": self.run_id,
            "scope_id": self.scope_id,
            "model_profile_ref": self.model_profile_ref,
            "stable_prompt_hash": self.stable_prompt_hash,
            "sections": [s.to_payload() for s in self.sections],
            "budget": {
                "context_window": self.context_window_tokens,
                "reserved_output": self.reserved_output_tokens,
                "available_input": self.available_input_tokens,
            },
            "planned_tokens": self.planned_tokens,
            "projected_tokens": self.projected_tokens,
            "actual": {
                "input_tokens": self.actual_input_tokens,
                "output_tokens": self.actual_output_tokens,
                "usage_ref": self.actual_usage_ref,
            },
            "supersedes": self.supersedes,
        }

    def with_actual(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        usage_ref: str,
    ) -> "ContextManifest":
        """引擎在 usage.reported 后回填 Actual Token（返回新实例）。"""
        return ContextManifest(
            manifest_id=self.manifest_id,
            run_id=self.run_id,
            scope_id=self.scope_id,
            model_profile_ref=self.model_profile_ref,
            stable_prompt_hash=self.stable_prompt_hash,
            sections=self.sections,
            context_window_tokens=self.context_window_tokens,
            reserved_output_tokens=self.reserved_output_tokens,
            available_input_tokens=self.available_input_tokens,
            planned_tokens=self.planned_tokens,
            projected_tokens=self.projected_tokens,
            actual_input_tokens=input_tokens,
            actual_output_tokens=output_tokens,
            actual_usage_ref=usage_ref,
            supersedes=self.supersedes,
        )


def build_manifest(
    *,
    plan: Any,
    projected_tokens: int,
    run_id: str,
    scope_id: str,
    model_profile_ref: str,
    supersedes: str | None = None,
) -> ContextManifest:
    """从 ContextPlan 构建 Manifest。

    ``plan`` 是 ``ksadk.context_engine.models.ContextPlan``；Section 的
    ``selected_reason`` 取自 decisions（included/dropped + reason），未出现在
    decisions 中的 required 项标注 "required"。
    """
    decisions = {d.item_id: d for d in plan.decisions}
    sections: list[ManifestSection] = []
    for item in plan.selected:
        decision = decisions.get(item.item_id)
        reason = (decision.reason if decision else "") or "selected"
        action = decision.action if decision else "included"
        content = item.content if isinstance(item.content, str) else repr(item.content)
        sections.append(
            ManifestSection(
                item_id=item.item_id,
                kind=str(item.kind),
                source_refs=(str(item.source),),
                selected_reason=f"{action}:{reason}" if reason != "required" else "required",
                estimated_tokens=int(item.estimated_tokens or 0),
                included=True,
                content_hash="sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
                if content
                else "",
            )
        )
    for item_id, decision in decisions.items():
        if decision.action == "dropped":
            sections.append(
                ManifestSection(
                    item_id=item_id,
                    kind="dropped",
                    selected_reason=f"dropped:{decision.reason}",
                    estimated_tokens=0,
                    included=False,
                )
            )
    material = f"{run_id}:{plan.plan_id}:{plan.stable_prefix_hash}:{projected_tokens}"
    budget = plan.budget
    return ContextManifest(
        manifest_id=_manifest_id(material),
        run_id=run_id,
        scope_id=scope_id,
        model_profile_ref=model_profile_ref,
        stable_prompt_hash=str(plan.stable_prefix_hash or ""),
        sections=tuple(sections),
        context_window_tokens=int(budget.context_window_tokens) if budget else 0,
        reserved_output_tokens=int(budget.reserved_output_tokens) if budget else 0,
        available_input_tokens=int(budget.max_input_tokens) if budget else 0,
        planned_tokens=int(plan.planned_input_tokens or 0),
        projected_tokens=int(projected_tokens),
        supersedes=supersedes,
    )


__all__ = ["ContextManifest", "ManifestSection", "build_manifest"]
