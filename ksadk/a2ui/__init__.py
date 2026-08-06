"""A2UI(Agent 驱动 UI)— canonical:docs/A2UI-agent驱动UI技术方案.md。"""

from ksadk.a2ui.core import A2UICore
from ksadk.a2ui.models import (
    BASIC_CATALOG,
    BASIC_CATALOG_ID,
    A2UIValidationError,
    ActionReceipt,
    Component,
    PendingInteraction,
    Surface,
)
from ksadk.a2ui.renderer import A2UIRenderer, RenderedNode, submit_a2ui_action

__all__ = [
    "A2UICore",
    "A2UIRenderer",
    "RenderedNode",
    "submit_a2ui_action",
    "A2UIValidationError",
    "ActionReceipt",
    "BASIC_CATALOG",
    "BASIC_CATALOG_ID",
    "Component",
    "PendingInteraction",
    "Surface",
]
