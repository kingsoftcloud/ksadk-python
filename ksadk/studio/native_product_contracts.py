"""Freeze Studio goal contracts and observation policy independently of assembly."""

from __future__ import annotations

import hashlib
import math

from ksadk.harness.browser_adapters.native_vision import VisionObservationContext
from ksadk.harness.native.completion import AcceptanceItem, TaskContract
from ksadk.harness.native.ports import ToolRequest, WorkspaceObservationPolicy
from ksadk.harness.native.state import RunScope
from ksadk.harness.native_runtime import TaskContractInput
from ksadk.harness.stores.native_contract import RunStore
from ksadk.studio.contracts import ExecutionSpec
from ksadk.studio.native_final_shape import (
    ARTIFACT_VERIFIER_REF,
    ARTIFACT_VERIFIER_VERSION,
    BROWSER_VERIFIER_REF,
    BROWSER_VERIFIER_VERSION,
    EVIDENCE_VERIFIER_REF,
    EVIDENCE_VERIFIER_VERSION,
    declared_value_submission_schema,
    encode_exact_workspace_file,
    final_shape_contract_description,
    requested_exact_workspace_file,
    requested_final_shape,
)


def _vision_observation_context(
    request: ToolRequest, *, scope: RunScope, store: RunStore
) -> VisionObservationContext:
    """Use the same scoped RunStore as the main loop, including accepted steering."""
    if request.scope != scope:
        raise ValueError("native_vision_scope_mismatch")
    snapshot = store.load(scope, request.run_id)
    failed = []
    observation = snapshot.unresolved_interaction_observation
    if observation is not None and isinstance(observation.get("error"), str):
        failed.append(observation["error"])
    # Only host-validated reports can contribute repair observations. They are
    # observational data here, never a new completion verdict or tool grant.
    if snapshot.verification_tool_calls is not None and snapshot.verification is not None:
        for item in snapshot.verification.get("items", []):
            if isinstance(item, dict) and item.get("status") == "failed":
                summary = item.get("safe_summary")
                if isinstance(summary, str) and summary:
                    failed.append(summary)
    return VisionObservationContext(
        scope=scope,
        run_id=snapshot.run_id,
        goal=snapshot.goal,
        goal_revision=snapshot.goal_revision,
        failed_observations=tuple(failed),
    )


def studio_web_observation_policy() -> WorkspaceObservationPolicy:
    """Studio's web task profile; other hosts may pin their own source types."""
    return WorkspaceObservationPolicy(
        path_suffixes=(".html", ".htm", ".js", ".jsx", ".css", ".ts", ".tsx"),
    )


def _goal_contract(facts: TaskContractInput) -> TaskContract:
    """Freeze the current goal while keeping absent host evidence unverified."""
    shape = requested_final_shape(facts.goal)
    submission_schema = declared_value_submission_schema(facts.goal)
    exact_file = requested_exact_workspace_file(facts.goal)
    revision = hashlib.sha256(
        f"{facts.session_id}\0{facts.parent_run_id or ''}\0{facts.goal}\0"
        f"{EVIDENCE_VERIFIER_REF}\0{EVIDENCE_VERIFIER_VERSION}".encode()
    ).hexdigest()[:24]
    items = [
        AcceptanceItem(
            acceptance_id="current_user_goal",
            verifier_ref=EVIDENCE_VERIFIER_REF,
            verifier_version=EVIDENCE_VERIFIER_VERSION,
            # Installed tools grant no acceptance authority. The verifier
            # selects applicable rejections from actual current-Run evidence;
            # absence of semantic evidence remains not_run, never passed.
            verification_method="deterministic",
            # Preserve the explicit output clause in the bounded contract;
            # it need not be the last sentence of a long user request.
            description=final_shape_contract_description(facts.goal)
            if shape
            else facts.goal[:1024],
        )
    ]
    if exact_file:
        items.append(
            AcceptanceItem(
                acceptance_id="exact_workspace_file",
                verifier_ref=ARTIFACT_VERIFIER_REF,
                verifier_version=ARTIFACT_VERIFIER_VERSION,
                verification_method="deterministic",
                description=encode_exact_workspace_file(exact_file),
            )
        )
    return TaskContract(
        revision=f"studio-goal-{revision}",
        items=tuple(items),
        # A general delivery contract, never a fixture-specific answer schema.
        # The Kernel submits this through its existing host final protocol and
        # still invokes the independent verifier. No text stripping is needed.
        final_submission_schema=submission_schema,
        final_submission_payload_key="output" if submission_schema is not None else None,
    )


def _activation_contract(activation_key: str, *, browser_available: bool) -> TaskContract:
    """Keep the immutable activation anchor stable across per-goal routing changes.

    Studio always supplies task_contract_factory for real user turns. Changing
    this legacy seed would change configuration.digest and reject a cold attach
    to an existing Run, even though its locked Build and goal are unchanged.
    This anchor has no authority to compile or verify a new user goal.
    """
    goal = "Initial activation contract"
    revision = hashlib.sha256(f"{activation_key}\0\0{goal}".encode()).hexdigest()[:24]
    return TaskContract(
        revision=f"studio-goal-{revision}",
        items=(
            AcceptanceItem(
                acceptance_id="current_user_goal",
                verifier_ref=BROWSER_VERIFIER_REF
                if browser_available
                else "studio.independent-evidence-unavailable",
                verifier_version=BROWSER_VERIFIER_VERSION if browser_available else "1",
                verification_method="deterministic" if browser_available else "human",
                description=goal,
            ),
        ),
    )


def _wall_delivery_seconds(execution: ExecutionSpec, *, effect_timeout_seconds: float) -> int:
    """Reserve two model attempts inside the existing deadline, never extend it.

    v1 remains byte-compatible for already sealed Builds. v2 allows time for
    planning a write and answering after its receipt; slow tools or providers
    can still exhaust the hard deadline. At least half remains for research.
    """
    if execution.native_wall_delivery_policy == "deadline-delivery/v1":
        return min(60, execution.timeout_seconds // 10)
    if execution.native_wall_delivery_policy in {
        "deadline-delivery/v2",
        "deadline-delivery/v3",
        "deadline-delivery/v4",
        "deadline-delivery/v5",
        "deadline-delivery/v6",
        "deadline-delivery/v7",
    }:
        return min(240, execution.timeout_seconds // 2, math.ceil(2 * effect_timeout_seconds))
    return 0
