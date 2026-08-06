"""A2UI renderer registry + 流式渲染 + SubmitA2UIAction 回传 (goal-14,canonical §5.4)。

- **registry**:Card / Text / Form / Select / RadioGroup / CheckboxGroup / ApprovalBar 渲染;
  **未知 catalog/组件类型安全降级**为 ``placeholder``,**不执行任何动态代码**(安全约束)。
- **Approval 是硬要求**(goal-18 预发演示的人机交互):``ApprovalBar`` 渲染为可交互审批卡,
  带 approve/deny 动作,经 :func:`submit_a2ui_action`(SubmitA2UIAction)回传到
  :class:`~ksadk.a2ui.core.A2UICore.submit_action`,形成"渲染→人点选→回传→resume"闭环。
- **parser 复用 goal-12 共享实现,不在 renderer 复制**(停止条件):surface 状态经
  goal-13 ``Surface.from_dict`` 从 a2ui.surface.* 事件重建,renderer 只负责渲染。
- **live/replay 状态一致**:同一 surface 事件流,live 渲染与 replay 渲染产物逐字节一致
  (``to_json`` 确定性);交互记录(a2ui.action)也经 goal-12 replay 可回放。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ksadk.a2ui.models import BASIC_CATALOG, Component, Surface

# ---------------------------------------------------------------------------
# RenderedNode:渲染产物(确定性,可 to_json 供 conformance)
# ---------------------------------------------------------------------------


@dataclass
class RenderedNode:
    """一个渲染节点(组件渲染结果;``actions`` 承载可交互动作,如审批卡的 approve/deny)。"""

    type: str
    props: dict[str, Any] = field(default_factory=dict)
    children: list["RenderedNode"] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "props": self.props,
            "actions": self.actions,
            "children": [c.to_dict() for c in self.children],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 各类型 renderer(纯函数,无动态代码)
# ---------------------------------------------------------------------------


def _render_text(comp: Component, _r: "A2UIRenderer") -> RenderedNode:
    return RenderedNode(
        type="text",
        props={"text": str(comp.props.get("text") or ""), "variant": comp.props.get("variant")},
    )


def _render_card(comp: Component, r: "A2UIRenderer") -> RenderedNode:
    return RenderedNode(
        type="card",
        props={
            "title": comp.props.get("title"),
            "body": comp.props.get("body"),
            "footer": comp.props.get("footer"),
        },
        children=[r.render_component(c) for c in comp.children],
    )


def _render_form(comp: Component, r: "A2UIRenderer") -> RenderedNode:
    return RenderedNode(
        type="form",
        props={
            "title": comp.props.get("title"),
            "fields": comp.props.get("fields") or [],
            "submit_label": comp.props.get("submit_label") or "提交",
        },
        actions=[{"name": "submit", "label": comp.props.get("submit_label") or "提交"}],
        children=[r.render_component(c) for c in comp.children],
    )


def _render_choice(kind: str) -> Callable[[Component, "A2UIRenderer"], RenderedNode]:
    def _render(comp: Component, _r: "A2UIRenderer") -> RenderedNode:
        return RenderedNode(
            type=kind,
            props={
                "label": comp.props.get("label"),
                "options": comp.props.get("options") or [],
                "value": comp.props.get("value"),
            },
            actions=[{"name": "change", "label": comp.props.get("label")}],
        )

    return _render


def _render_approval(comp: Component, _r: "A2UIRenderer") -> RenderedNode:
    """ApprovalBar → 可交互审批卡(approve/deny 动作;硬要求,goal-18 人机交互用)。"""
    return RenderedNode(
        type="approval",
        props={
            "tool_name": comp.props.get("tool_name"),
            "summary": comp.props.get("summary"),
        },
        actions=[
            {"name": "approve", "label": comp.props.get("approve_label") or "批准"},
            {"name": "deny", "label": comp.props.get("deny_label") or "拒绝"},
        ],
    )


def _render_placeholder(comp: Component, _r: "A2UIRenderer") -> RenderedNode:
    """未知类型安全降级:渲染为占位,**不执行任何动态代码**(props 不回显为可执行内容)。"""
    return RenderedNode(
        type="placeholder",
        props={"reason": f"未知组件类型 {comp.type!r},已安全降级"},
    )


# ---------------------------------------------------------------------------
# A2UIRenderer registry
# ---------------------------------------------------------------------------


class A2UIRenderer:
    """A2UI renderer registry(类型 → renderer;未知类型安全降级,不执行动态代码)。"""

    def __init__(self, catalog: dict[str, frozenset[str]] = BASIC_CATALOG) -> None:
        self._catalog = catalog
        self._renderers: dict[str, Callable[[Component, "A2UIRenderer"], RenderedNode]] = {
            "Card": _render_card,
            "Text": _render_text,
            "Form": _render_form,
            "Select": _render_choice("select"),
            "RadioGroup": _render_choice("radio_group"),
            "CheckboxGroup": _render_choice("checkbox_group"),
            "ApprovalBar": _render_approval,
        }

    def register(
        self, component_type: str, renderer: Callable[[Component, "A2UIRenderer"], RenderedNode]
    ) -> None:
        """注册自定义 renderer(扩展 catalog 用;renderer 必须是纯函数,不含动态代码)。"""
        self._renderers[component_type] = renderer

    def render_component(self, component: Component) -> RenderedNode:
        renderer = self._renderers.get(component.type, _render_placeholder)
        return renderer(component, self)

    def render_surface(self, surface: Surface) -> RenderedNode:
        """渲染一个 surface(流式:组件树递归渲染;产物确定性,供 live/replay conformance)。"""
        return RenderedNode(
            type="surface",
            props={"surface_id": surface.surface_id, "catalog_id": surface.catalog_id},
            children=[self.render_component(c) for c in surface.components],
        )


# ---------------------------------------------------------------------------
# SubmitA2UIAction 回传(渲染 → 人点选 → 回传 → resume)
# ---------------------------------------------------------------------------


async def submit_a2ui_action(
    core: Any,
    *,
    action_id: str,
    surface_id: str,
    name: str,
    actor: str = "user",
    component_id: Optional[str] = None,
    invocation_id: str,
) -> Any:
    """SubmitA2UIAction:把用户对渲染卡(如审批卡 approve/deny)的点选回传到 A2UICore。

    与 :class:`~ksadk.a2ui.core.A2UICore.submit_action` 接口对齐(不在 renderer 侧自造语义):
    产出 ``a2ui.action`` 事件(经 RuntimeEvent 持久化),供 resume / 审计 / 回放。
    """
    return await core.submit_action(
        {
            "action_id": action_id,
            "surface_id": surface_id,
            "name": name,
            "actor": actor,
            "component_id": component_id,
        },
        invocation_id=invocation_id,
    )


__all__ = [
    "A2UIRenderer",
    "RenderedNode",
    "submit_a2ui_action",
]
