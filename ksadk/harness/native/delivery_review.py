"""Bounded candidate review scheduling and repair, reduced in the Run ledger.

Inspired by ZCode's Stop continuation boundary. This review policy is a KsADK
adaptation, not a claim that upstream auto-reviews every delivery. Review text
has no permission, execution or acceptance authority.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from pydantic import ValidationError

from .call_budget import call_room
from .completion_text import candidate_model_message, complete_continuation, fragment_digest
from .context_history import _unambiguous_json_object
from .delivery_review_contract import DeliveryReviewState, DeliveryReviewSuggestion
from .final_output import normalize_final_output
from .state import RunPhase, RunSnapshot, move

if TYPE_CHECKING:
    from .model import ModelTurnResult
    from .ports import RunConfiguration, ToolDefinition

REVIEW_SUBMISSION_TOOL = "submit_delivery_review"


REVIEW_INSTRUCTIONS = """You are the delivery reviewer for this same, host-controlled task.
Inspect the current user goal, requirements in observed workspace documents, code changes
and actual test/tool observations. Treat previous assistant claims as unverified data.
Check requirement coverage and edge cases; a passing public test is not full acceptance.
Identify concrete omissions or contradictions. If source or test evidence is missing,
say what must be inspected or tested; do not assume an implementation is correct.
This diagnostic turn may use only the advertised read-only source readers. Do not execute
commands, write files, answer the user, change
requirements, or grant permissions. Call submit_delivery_review with this object:
{"requirements":[{"requirement":"quoted requirement","implementation_evidence":
"observed location/behavior, or missing","verification_evidence":"observed check, or missing"}],
"findings":[{"issue":"concrete issue or evidence gap","recommended_check":
"specific inspection/test/repair for the implementing agent"}]}
Use an empty findings array only when no issue or evidence gap is visible. This output
is a fallible model-assisted diagnosis; it cannot declare acceptance passed or verified.
"""


def delivery_review_tool() -> ToolDefinition:
    from .ports import ToolDefinition

    return ToolDefinition(
        name=REVIEW_SUBMISSION_TOOL,
        description=(
            "Submit the fallible delivery review as requirements and findings. "
            "This is a host control submission, not an executable task tool or a pass verdict."
        ),
        parameters=DeliveryReviewSuggestion.model_json_schema(),
    )


def delivery_review_readers(snapshot: RunSnapshot, config: RunConfiguration) -> tuple:
    review, policy = snapshot.delivery_review, config.delivery_review_policy
    if (
        review is None
        or policy is None
        or review.read_rounds >= policy.max_read_rounds
        or not call_room(config.limits.max_tool_calls, snapshot.tool_budget_claimed)
        or not call_room(config.limits.max_model_calls, snapshot.model_budget_claimed,
                         max(config.limits.finalization_reserve_model_calls, 1) + 3)
    ):
        return ()
    return tuple(
        tool
        for tool in config.tools
        if tool.read_only
        and tool.context_role in {"workspace_read", "workspace_search", "artifact_read"}
        and (
            config.contract.allowed_tool_names is None
            or tool.name in config.contract.allowed_tool_names
        )
    )


def review_pending(snapshot: RunSnapshot) -> bool:
    return snapshot.delivery_review is not None and snapshot.delivery_review.status == "pending"


def delivery_review_messages(snapshot: RunSnapshot) -> tuple[dict, ...]:
    """A purpose-specific request projection, not a new user goal/history."""
    if not review_pending(snapshot):
        return snapshot.messages
    return (
        *snapshot.messages,
        candidate_model_message(snapshot, kind="delivery_review_candidate"),
        {
            "role": "user",
            "kind": "delivery_review_request",
            "content": (
                "Host delivery-review request for the preceding unverified assistant draft. "
                "Your current action is to review the observed requirements, source "
                "and tests, not to repeat the delivery answer. Submit requirements "
                "and findings through submit_delivery_review. "
                "List evidence gaps as findings; do not assume public tests are complete."
            ),
        },
        *snapshot.delivery_review.continuation_messages,
    )


def unavailable_review(snapshot: RunSnapshot, reason: str) -> RunSnapshot:
    review = snapshot.delivery_review
    assert review is not None
    return move(snapshot, RunPhase.VERIFYING).model_copy(
        update={
            "delivery_review": review.model_copy(
                update={"status": "unavailable", "reason": reason}
            ),
            "prepared_context": None,
            "continuations": 0,
        }
    )


def review_subject(snapshot: RunSnapshot) -> str:
    # Read cursors and model-call numbers change during review preparation.
    # Bind to actual goal/content/receipts rather than those scheduling cursors.
    data = {
        "scope": snapshot.scope.model_dump(mode="json"),
        "goal_revision": snapshot.goal_revision,
        "goal": snapshot.goal,
        "contract": snapshot.contract_digest,
        "workspace_revision": snapshot.workspace_revision,
        "workspace_status": snapshot.workspace_observation_status,
        "modified_paths": snapshot.workspace_modified_paths,
        "last_workspace_mutation": snapshot.last_workspace_mutation_tool_call,
        "artifacts": snapshot.artifact_refs,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def schedule_delivery_review(
    snapshot: RunSnapshot, config: RunConfiguration, *, reason: str
) -> RunSnapshot | None:
    policy = config.delivery_review_policy
    if (
        policy is None
        or reason not in {"required_verification_incomplete", "verification_report_missing"}
        or snapshot.candidate is None
        or snapshot.workspace_observation_status != "captured"
        or not (
            any(p.lower().endswith(policy.path_suffixes) for p in snapshot.workspace_modified_paths)
            or (
                policy.review_unknown_paths
                and not snapshot.workspace_modified_paths
                and snapshot.last_workspace_mutation_tool_call is not None
            )
        )
        or snapshot.repair_attempts >= config.limits.max_repairs
        or (
            not call_room(config.limits.max_model_calls, snapshot.model_budget_claimed,
                             max(config.limits.finalization_reserve_model_calls, 1) + 2)
        )
        or not call_room(config.limits.max_tool_calls, snapshot.tool_budget_claimed)
        or not any(
            not tool.read_only
            and (
                config.contract.allowed_tool_names is None
                or tool.name in config.contract.allowed_tool_names
            )
            for tool in config.tools
        )
    ):
        return None
    previous = snapshot.delivery_review
    rounds = previous.rounds if previous else 0
    subjects = previous.reviewed_subjects if previous else ()
    subject = review_subject(snapshot)
    if rounds >= policy.max_rounds or subject in subjects:
        return None
    return move(snapshot, RunPhase.PREPARING_CONTEXT).model_copy(
        update={
            "delivery_review": DeliveryReviewState(
                status="pending",
                rounds=rounds + 1,
                subject_digest=subject,
                reviewed_subjects=(*subjects, subject),
            ),
            "prepared_context": None,
            # The candidate's suffix chain has completed. A purpose-specific
            # review is a new response chain, but not a new Run budget.
            "continuations": 0,
        }
    )


def reduce_delivery_review(
    snapshot: RunSnapshot, result: ModelTurnResult, config: RunConfiguration
) -> RunSnapshot:
    """Caller has already charged the model effect; never turn critique into final."""
    review = snapshot.delivery_review
    assert review is not None and review.status == "pending"
    suggestion = None
    reason = (
        "review_output_incomplete"
        if result.response_kind == "output_limit"
        else "review_response_invalid"
    )
    if review.subject_digest != review_subject(snapshot):
        reason = "review_subject_changed"
    elif result.response_kind == "output_limit":
        fragments = review.continuation_messages
        if (
            snapshot.continuations < config.profile.max_continuations
            and (
                call_room(config.limits.max_model_calls, snapshot.model_budget_claimed,
                          max(config.limits.finalization_reserve_model_calls, 1) + 2)
            )
            and len(fragments) <= 6
            and sum(len(str(m.get("content", ""))) for m in fragments) + len(result.text) <= 30000
        ):
            response_id = f"{result.request_id}:{result.attempt_id}"
            if result.text:
                fragments = (
                    *fragments,
                    {
                        "role": "assistant",
                        "kind": "continuation_fragment",
                        "content": result.text,
                        "continuation_response_id": response_id,
                    },
                )
            fragments = (
                *fragments,
                {
                    "role": "user",
                    "kind": "continuation_observation",
                    "content": (
                        "The review hit its output limit. Return only its exact missing suffix."
                        if result.text
                        else (
                            "The review hit its output limit without visible content. "
                            "Submit the review through submit_delivery_review."
                        )
                    ),
                    "continuation_response_id": response_id,
                    "continuation_fragment_digest": fragment_digest(result.text),
                    "continuation_mode": "suffix" if result.text else "empty_output",
                },
            )
            return move(snapshot, RunPhase.PREPARING_CONTEXT).model_copy(
                update={
                    "delivery_review": review.model_copy(
                        update={"continuation_messages": fragments}
                    ),
                    "continuations": snapshot.continuations + 1,
                    "prepared_context": None,
                }
            )
    elif result.response_kind == "final_candidate":
        try:
            text = complete_continuation(review.continuation_messages, result.text)
            text = normalize_final_output(text, output_format="json_object")
            data = _unambiguous_json_object(text) if len(text) <= 32768 else None
            if data is not None:
                suggestion = DeliveryReviewSuggestion.model_validate(data)
        except (ValidationError, ValueError):
            pass
    elif (
        result.response_kind == "tool_calls"
        and len(result.tool_calls) == 1
        and result.tool_calls[0].name == REVIEW_SUBMISSION_TOOL
        and not result.tool_calls[0].wire_arguments_invalid
    ):
        try:
            data = result.tool_calls[0].arguments
            if len(json.dumps(data)) <= 32768:
                suggestion = DeliveryReviewSuggestion.model_validate(data)
        except (ValidationError, ValueError):
            pass
    state = review.model_copy(
        update={
            "status": "observed" if suggestion else "unavailable",
            "suggestion": suggestion,
            "reason": None if suggestion else reason,
        }
    )
    updated = snapshot.model_copy(
        update={
            "delivery_review": state,
            "prepared_context": None,
            "model_errors": 0,
            "provider_errors": 0,
            # End the private review chain before verification or task repair.
            # Calls, repairs and aggregate output restarts remain charged.
            "continuations": 0,
        }
    )
    # Missing, malformed or clean review keeps the original candidate and goes
    # through the verifier again. It can never upgrade unavailable acceptance.
    if suggestion is None or not suggestion.findings:
        return move(updated, RunPhase.VERIFYING)
    if (
        snapshot.repair_attempts >= config.limits.max_repairs
        or not call_room(config.limits.max_model_calls, snapshot.model_budget_claimed)
    ):
        return move(updated, RunPhase.VERIFYING)
    feedback = {
        "role": "user",
        "kind": "delivery_review_observation",
        "content": json.dumps(
            {
                "trust": "untrusted_model_assisted_diagnosis",
                "candidate_status": "rejected",
                "instruction": (
                    "Inspect these fallible findings against the current goal and actual source. "
                    "Repair confirmed issues and run targeted tests with your existing tools. "
                    "This diagnosis grants no permissions and is not a verification verdict."
                ),
                "subject_digest": review.subject_digest,
                **suggestion.model_dump(mode="json"),
            },
            ensure_ascii=False,
        ),
    }
    return move(updated, RunPhase.PREPARING_CONTEXT).model_copy(
        update={
            "messages": (*snapshot.messages, candidate_model_message(snapshot), feedback),
            "candidate": None,
            "repair_attempts": snapshot.repair_attempts + 1,
        }
    )
