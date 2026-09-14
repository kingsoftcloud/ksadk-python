"""Trusted, invocation-scoped policy extension for the managed Agent Loop.

Only the host resolver grants authority. The model sees tool schemas and the
resolved system context, never the opaque reference or the resolver itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Protocol

from ksadk.harness.spec import ExecutionStrategySpec, HarnessSpec, PromptSpec
from ksadk.runtime import StartRequest


@dataclass(frozen=True)
class ExecutionPolicy:
    system_context: str = ""
    tools: Mapping[str, Any] = field(default_factory=dict)
    workspace_root: Path | None = None
    limits: Mapping[str, int] = field(default_factory=dict)
    approval_required: frozenset[str] = frozenset()
    # None preserves the parent's context; an explicit empty string suppresses
    # that prompt suffix. This changes model context, never execution authority.
    child_system_context: str | None = None

    def __post_init__(self) -> None:
        if self.child_system_context is not None and not isinstance(self.child_system_context, str):
            raise ValueError("child_system_context must be a string or None")
        allowed = {"max_total_tokens", "max_tool_calls", "max_artifacts", "max_model_calls"}
        if set(self.limits) - allowed:
            raise ValueError("unsupported execution policy budget")
        if any(
            isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in self.limits.values()
        ):
            raise ValueError("execution policy limits must be non-negative integers")
        if any(not isinstance(name, str) or not name for name in self.tools):
            raise ValueError("execution policy tools must have non-empty names")
        object.__setattr__(self, "tools", MappingProxyType(dict(self.tools)))
        object.__setattr__(self, "limits", MappingProxyType(dict(self.limits)))
        object.__setattr__(self, "approval_required", frozenset(self.approval_required))
        if self.workspace_root is not None:
            object.__setattr__(self, "workspace_root", Path(self.workspace_root).resolve())


class ExecutionPolicyResolver(Protocol):
    async def resolve(self, ref: str, *, request: StartRequest) -> ExecutionPolicy:
        """Validate scope/authority on every call; reject expired or revoked refs."""
        ...


_CURRENT_POLICY: ContextVar[ExecutionPolicy | None] = ContextVar(
    "ksadk_harness_execution_policy", default=None
)


def current_execution_policy() -> ExecutionPolicy | None:
    return _CURRENT_POLICY.get()


@contextmanager
def execution_policy_scope(policy: ExecutionPolicy | None) -> Iterator[None]:
    token = _CURRENT_POLICY.set(policy)
    try:
        yield
    finally:
        _CURRENT_POLICY.reset(token)


def apply_execution_policy(spec: HarnessSpec, policy: ExecutionPolicy) -> HarnessSpec:
    """Derive a Run spec; never mutate the shared immutable Revision."""
    config = dict(spec.execution_strategy.config)
    for name, limit in policy.limits.items():
        previous = config.get(name)
        config[name] = min(int(previous), limit) if previous is not None else limit
    instructions = spec.prompt.instructions or ""
    if policy.system_context:
        instructions += "\n\n" + policy.system_context
    return spec.model_copy(
        update={
            "prompt": PromptSpec(instructions=instructions),
            "execution_strategy": ExecutionStrategySpec(
                kind=spec.execution_strategy.kind, config=config
            ),
        }
    )


__all__ = ["ExecutionPolicy", "ExecutionPolicyResolver", "current_execution_policy"]
