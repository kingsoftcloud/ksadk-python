"""编排上下文。"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


@dataclass
class OrchestrationContext:
    """用于在编排 agent 之间共享和分支状态。"""

    session_id: str = ""
    state: dict[str, Any] = field(default_factory=dict)
    branch: str = ""
    parent_context: "OrchestrationContext | None" = None

    def get(self, key: str, default: Any = None) -> Any:
        return self.state.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.state[key] = value

    def update(self, values: dict[str, Any]) -> None:
        self.state.update(values)

    def snapshot_state(self) -> dict[str, Any]:
        return copy.deepcopy(self.state)

    def create_branch(self, branch_name: str) -> "OrchestrationContext":
        branch = f"{self.branch}.{branch_name}" if self.branch else branch_name
        return OrchestrationContext(
            session_id=self.session_id,
            state=self.snapshot_state(),
            branch=branch,
            parent_context=self,
        )
