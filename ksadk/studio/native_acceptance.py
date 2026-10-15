"""Trusted host seam for Native task acceptance in an ordinary Studio Run.

The selected Build cannot provide executable verifier code. A Studio host may
install an extension alongside its Native composer, after Build resolution and
scope binding. This keeps task-specific checks out of the Native Kernel while
allowing their evidence to enter its normal verify/repair loop.
"""

from __future__ import annotations

import hashlib
from typing import Protocol

from ksadk.harness.native.completion import TaskContract
from ksadk.harness.native.ports import VerificationPort
from ksadk.harness.native.state import RunScope
from ksadk.harness.native_runtime import TaskContractInput
from ksadk.plugins.providers.harness import HarnessEngineActivationContext


class StudioNativeAcceptanceExtension(Protocol):
    """Versioned contract and verifier supplied by the trusted Studio host."""

    version: str

    def compile_contract(
        self, facts: TaskContractInput, baseline: TaskContract
    ) -> TaskContract: ...

    def bind_verifier(
        self,
        *,
        context: HarnessEngineActivationContext,
        scope: RunScope,
        baseline: VerificationPort,
    ) -> VerificationPort: ...


def extend_contract(
    facts: TaskContractInput,
    baseline: TaskContract,
    extension: StudioNativeAcceptanceExtension,
) -> TaskContract:
    """Keep current-goal identity and bind extension rules to this turn."""
    version = getattr(extension, "version", None)
    if not isinstance(version, str) or not version.strip():
        raise ValueError("native_studio_acceptance_version_missing")
    proposed = extension.compile_contract(facts, baseline)
    if not isinstance(proposed, TaskContract):
        raise ValueError("native_studio_acceptance_contract_invalid")
    # model_copy(update=...) does not validate Pydantic fields. Reparse the
    # host extension's proposed contract before it can become a durable goal.
    proposed = TaskContract.model_validate_json(proposed.model_dump_json())
    goal = baseline.items[0]
    if not any(
        item.acceptance_id == goal.acceptance_id and item.description == goal.description
        for item in proposed.items
    ):
        raise ValueError("native_studio_acceptance_goal_missing")
    if len({item.acceptance_id for item in proposed.items}) != len(proposed.items):
        raise ValueError("native_studio_acceptance_duplicate_id")
    digest = hashlib.sha256(
        f"{version}\0{proposed.model_dump_json()}".encode()
    ).hexdigest()[:20]
    return proposed.model_copy(update={"revision": f"{baseline.revision}+{digest}"})


__all__ = ["StudioNativeAcceptanceExtension", "extend_contract"]
