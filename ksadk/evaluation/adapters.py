"""Target-kind routing for evaluation adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from .contracts import EvalCase, EvalRunSpec, TargetKind, TargetRef, TargetRun, TargetSnapshot

if TYPE_CHECKING:
    from .evidence import EvidenceStore


class TargetAdapterError(RuntimeError):
    """Classified failure raised by a protocol adapter."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class EvaluationNotImplementedError(NotImplementedError):
    """Raised when a target kind has no evaluation adapter yet."""


class TargetAdapter(Protocol):
    """Protocol-specific execution behind the common target lifecycle."""

    kind: TargetKind

    async def snapshot(self, target: TargetRef) -> TargetSnapshot:
        """Resolve a target into an immutable snapshot."""

    async def run_case(self, spec: EvalRunSpec, case: EvalCase, *, attempt: int) -> TargetRun:
        """Execute one case and return its normalized result."""


def create_target_adapter(
    target: TargetRef,
    *,
    timeout_seconds: int,
    evidence_store: EvidenceStore | None = None,
) -> TargetAdapter:
    """Route a target reference to its protocol adapter."""

    if target.kind is TargetKind.A2A:
        from .a2a_adapter import A2ATargetAdapter

        return A2ATargetAdapter(timeout_seconds=timeout_seconds)
    if target.kind is TargetKind.LOCAL_SOURCE:
        from .local_adapter import LocalSourceTargetAdapter

        return LocalSourceTargetAdapter(
            timeout_seconds=timeout_seconds,
            evidence_store=evidence_store,
        )
    raise EvaluationNotImplementedError(
        f"{target.kind.value} target 的评测执行尚未实现；"
        "当前可使用 --validate-only 校验评测集和参数"
    )


__all__ = [
    "EvaluationNotImplementedError",
    "TargetAdapterError",
    "TargetAdapter",
    "create_target_adapter",
]
