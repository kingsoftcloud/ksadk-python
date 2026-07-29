"""A2UI 语义模型 + basic catalog (goal-13,canonical 见 docs/A2UI-agent驱动UI技术方案.md)。

模型独立于 transport(RuntimeEvent 是 canonical;Responses/A2A 只是 adapter):

- ``Component`` / ``Surface``:声明式组件树 + 数据模型(props 是**数据**,不含可执行代码)。
- ``PendingInteraction``:一次 ``request_ui_input`` 的持久化身份(input_required 语义)。
- ``ActionReceipt``:一次 action 的幂等回执。
- ``BASIC_CATALOG``:Q3 MVP 渲染集(Card/Text/Form/Select/RadioGroup/CheckboxGroup/
  ApprovalBar)。**未知组件类型安全降级**(渲染为占位,不执行任意代码),符合设计 §3.1/§3.2。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

#: pinned A2UI core 版本(goal-13 版本约束:a2ui-core==0.1.1,A2UI v1.0 Candidate)。
#: 校验期据此断言构建期 pinned 版本未漂移(Q4 治理:upstream commit + schema/catalog SHA)。
try:
    from a2ui.core.version import __version__ as A2UI_CORE_VERSION
except ImportError:  # pragma: no cover - a2ui-core 未安装时显式降级
    A2UI_CORE_VERSION = "uninstalled"

# ---------------------------------------------------------------------------
# basic catalog(Q3 MVP 渲染集;未知类型安全降级)
# ---------------------------------------------------------------------------

#: 允许展示/交互的组件类型及其允许的 prop 键(白名单;props 是数据,不允许代码)。
BASIC_CATALOG: dict[str, frozenset[str]] = {
    "Card": frozenset({"title", "body", "footer", "children"}),
    "Text": frozenset({"text", "variant"}),
    "Form": frozenset({"title", "fields", "submit_label"}),
    "Select": frozenset({"label", "options", "value"}),
    "RadioGroup": frozenset({"label", "options", "value"}),
    "CheckboxGroup": frozenset({"label", "options", "value"}),
    "ApprovalBar": frozenset({"tool_name", "summary", "approve_label", "deny_label"}),
}

#: catalog 标识(对应 pinned basic catalog;构建期校验 SHA 是后续治理项)。
BASIC_CATALOG_ID = "a2ui.org/catalogs/basic/v1.0"


class A2UIValidationError(ValueError):
    """surface/component 校验失败(未知类型、非法 prop、结构错误)。"""


@dataclass
class Component:
    """声明式组件(props 是数据,不可执行)。"""

    component_id: str
    type: str
    props: dict[str, Any] = field(default_factory=dict)
    children: list["Component"] = field(default_factory=list)

    def validate(self, catalog: dict[str, frozenset[str]] = BASIC_CATALOG) -> None:
        """校验组件类型已知 + prop 在白名单 + 无嵌套代码字段。未知类型 → 安全降级错误。"""
        if self.type not in catalog:
            raise A2UIValidationError(
                f"未知组件类型 {self.type!r}(不在 basic catalog;安全降级,不渲染任意代码)"
            )
        allowed = catalog[self.type]
        unknown_props = set(self.props.keys()) - allowed - {"children"}
        if unknown_props:
            raise A2UIValidationError(f"组件 {self.type} 含白名单外 prop: {sorted(unknown_props)}")
        for key, value in self.props.items():
            if callable(value):
                raise A2UIValidationError(f"组件 {self.type} prop {key} 不可为可执行代码")
        for child in self.children:
            child.validate(catalog)

    def to_dict(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "type": self.type,
            "props": self.props,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Component":
        """从 dict(a2ui.surface.* 事件 payload 中的组件)重建;供 renderer 反序列化。"""
        return cls(
            component_id=str(data.get("component_id") or ""),
            type=str(data.get("type") or ""),
            props=dict(data.get("props") or {}),
            children=[cls.from_dict(c) for c in data.get("children") or []],
        )


@dataclass
class Surface:
    """一个 UI surface(组件树 + 数据模型)。"""

    surface_id: str
    components: list[Component] = field(default_factory=list)
    data_model: dict[str, Any] = field(default_factory=dict)
    catalog_id: str = BASIC_CATALOG_ID

    @classmethod
    def new(cls, components: list[Component], *, data_model: Optional[dict] = None) -> "Surface":
        return cls(
            surface_id=f"surf_{uuid.uuid4().hex[:12]}",
            components=components,
            data_model=data_model or {},
        )

    def validate(self, catalog: dict[str, frozenset[str]] = BASIC_CATALOG) -> None:
        if not self.surface_id:
            raise A2UIValidationError("surface_id 不能为空")
        for component in self.components:
            component.validate(catalog)

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface_id": self.surface_id,
            "catalog_id": self.catalog_id,
            "components": [c.to_dict() for c in self.components],
            "data_model": self.data_model,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Surface":
        """从 dict(a2ui.surface.* 事件 payload["surface"])重建 Surface;供 renderer 反序列化。"""
        return cls(
            surface_id=str(data.get("surface_id") or ""),
            components=[Component.from_dict(c) for c in data.get("components") or []],
            data_model=dict(data.get("data_model") or {}),
            catalog_id=str(data.get("catalog_id") or BASIC_CATALOG_ID),
        )


@dataclass
class PendingInteraction:
    """一次 request_ui_input 的持久化身份(input_required;命中后 resume)。"""

    interaction_id: str
    surface_id: str
    kind: str  # "form" | "select" | "approval" | ...
    input_schema: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"  # "pending" | "resolved" | "expired"


@dataclass
class ActionReceipt:
    """一次 action 的幂等回执(审计用)。"""

    action_id: str
    surface_id: str
    name: str
    actor: str = "user"
    status: str = "received"  # "received" | "completed" | "failed"
    response: Optional[dict[str, Any]] = None
    error: Optional[str] = None


__all__ = [
    "A2UIValidationError",
    "ActionReceipt",
    "BASIC_CATALOG",
    "BASIC_CATALOG_ID",
    "Component",
    "PendingInteraction",
    "Surface",
]
