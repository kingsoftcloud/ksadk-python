"""Common target lifecycle shared by all evaluation adapters."""

from __future__ import annotations

import asyncio

from .adapters import (
    EvaluationNotImplementedError,
    TargetAdapter,
    TargetAdapterError,
    create_target_adapter,
)
from .contracts import (
    EvalCase,
    EvalRunSpec,
    EvaluationConfig,
    TargetRef,
    TargetRun,
    TargetRunStatus,
    TargetSnapshot,
)


class EvaluationExecutionError(RuntimeError):
    """A classified failure from the common target execution boundary."""


class EvaluationTarget:
    """Own target lifecycle; protocol details stay in the selected adapter."""

    def __init__(self, target: TargetRef, config: EvaluationConfig) -> None:
        self.reference = target
        self._timeout_seconds = config.timeout_seconds
        self._adapter: TargetAdapter = create_target_adapter(
            target, timeout_seconds=config.timeout_seconds
        )

    async def snapshot(self) -> TargetSnapshot:
        try:
            return await asyncio.wait_for(
                self._adapter.snapshot(self.reference),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as exc:
            raise EvaluationExecutionError(
                f"{self.reference.kind.value} Target 快照超时"
            ) from exc
        except TargetAdapterError as exc:
            raise EvaluationExecutionError(f"{exc.code}: {exc}") from exc

    async def run_case(
        self,
        spec: EvalRunSpec,
        case: EvalCase,
    ) -> TargetRun:
        try:
            return await asyncio.wait_for(
                self._adapter.run_case(spec, case, attempt=spec.attempt),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            return TargetRun(
                status=TargetRunStatus.ERROR,
                error_code="EVALUATION_CASE_TIMEOUT",
                error_message=f"Case {case.id} 执行超时",
            )


__all__ = [
    "EvaluationExecutionError",
    "EvaluationNotImplementedError",
    "EvaluationTarget",
]
