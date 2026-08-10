"""Single public handoff from CLI/Studio to the future evaluation executor."""

from __future__ import annotations

from typing import Protocol

from .contracts import (
    EvalCase,
    EvalRunReport,
    EvalRunSpec,
    EvaluationRequest,
    TargetKind,
    TargetRef,
    TargetRun,
    TargetSnapshot,
)


class EvaluationNotImplementedError(NotImplementedError):
    """Raised until a target adapter is registered with the executor."""


class EvaluationExecutionError(RuntimeError):
    """A safe, classified error that maps to the CLI execution exit code."""


class TargetAdapter(Protocol):
    """Minimum handoff owned by a Local/A2A/Codex target implementation."""

    kind: TargetKind

    async def snapshot(self, target: TargetRef) -> TargetSnapshot:
        """Resolve a CLI/Studio target reference into an immutable snapshot."""

    async def run_case(
        self, spec: EvalRunSpec, case: EvalCase, *, attempt: int
    ) -> TargetRun:
        """Execute one case and return only the normalized target result."""


async def execute_evaluation(request: EvaluationRequest) -> EvalRunReport:
    """Execute one evaluation request.

    Local, A2A, and Codex target implementations intentionally land behind this
    function so callers do not depend on adapter details.
    """

    raise EvaluationNotImplementedError(
        f"{request.target.kind.value} target 的评测执行尚未实现；"
        "当前可使用 --validate-only 校验评测集和参数"
    )
