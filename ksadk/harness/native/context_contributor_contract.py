"""Native admission of the existing canonical read-only Contributor wire.

The host supplies grants and requests. A producer supplies only a Response;
fragment roles cannot fabricate a user turn or executable tool protocol.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, field_validator

from ksadk.plugins.context_contributor import (
    AuthenticatedContextScope,
    ContextContributorCapabilities,
    ContextContributorExchange,
    ContextContributorRequest,
    ContextContributorResponse,
)

from .state import Contract


class ContextContributorPolicy(Contract):
    capabilities: ContextContributorCapabilities
    policy_ref: str
    allowed_classifications: tuple[str, ...]
    workspace_root: str = Field(max_length=4096)
    tokenizer_id: str = Field(min_length=1)

    @field_validator("policy_ref")
    @classmethod
    def pinned_policy(cls, value):
        return ContextContributorRequest.validate_policy_ref(value)

    @field_validator("allowed_classifications")
    @classmethod
    def classifications(cls, value):
        return ContextContributorRequest.validate_allowed_classifications(value)


class ContextModelDispatchDenial(Contract):
    """A host admission receipt, distinct from any provider response or usage."""

    schema_version: Literal["ksadk.native-context-model-denial/v1"] = (
        "ksadk.native-context-model-denial/v1"
    )
    operation_key: str = Field(min_length=1)
    reason: Literal["context_source_expired"] = "context_source_expired"


def contributor_request(collection, source, remaining_budget: int):
    policy = source.contributor_policy
    if policy is None:
        raise ValueError("Context source has no Contributor grant")
    return ContextContributorRequest(
        request_format="ksadk.context-request/v1",
        scope=AuthenticatedContextScope(
            authenticated=True, actor_id=collection.scope.user_id,
            agent_id=collection.scope.agent_id, session_id=collection.session_id,
            turn_id=collection.turn_id,
        ),
        invocation_id=collection.operation_key, user_input=collection.goal,
        workspace_root=policy.workspace_root, policy_ref=policy.policy_ref,
        remaining_budget=remaining_budget,
        allowed_classifications=policy.allowed_classifications,
        metadata={
            "tenant_id": collection.scope.tenant_id,
            "workspace_id": collection.scope.workspace_id,
            "run_id": collection.run_id, "goal_revision": collection.goal_revision,
        },
    )


def contributor_exchange(collection, source, response, remaining_budget: int):
    # Seal all producer-owned nested containers before constructing the wire.
    response = ContextContributorResponse.model_validate_json(response.model_dump_json())
    return ContextContributorExchange(
        contract_format="ksadk.context-contributor/v1",
        capabilities=source.contributor_policy.capabilities,
        request=contributor_request(collection, source, remaining_budget), response=response,
    )


def contributor_messages(collection, source, response):
    messages = []
    for fragment in response.fragments:
        role = fragment.metadata.get("role")
        privileged = fragment.kind == "compiled_prompt" or (
            isinstance(role, str) and role in {"system", "developer"}
        )
        # Canonical exchange admission checks both the host grant and fragment
        # trust. Other projection hints, including tool/history/current_input,
        # are data inside this envelope, never executable message protocol.
        projected_role = "system" if privileged else "user"
        messages.append({
            "role": projected_role, "kind": "context_contribution",
            "goal_revision": collection.goal_revision,
            "context_operation_key": collection.operation_key,
            "context_source_id": source.source_id,
            "context_fragment_id": fragment.item_id,
            "context_expires_at": (
                fragment.expires_at.isoformat() if fragment.expires_at is not None else None
            ),
            "content": json.dumps({
                "type": "ksadk.context-fragment-data/v1",
                "source_ref": source.source_ref, "binding_digest": source.binding_digest,
                "policy_ref": source.contributor_policy.policy_ref,
                "cacheability": source.contributor_policy.capabilities.cacheability,
                "fragment": fragment.model_dump(mode="json", by_alias=True),
            }, ensure_ascii=False, sort_keys=True),
        })
    return tuple(messages)


def utc_now():
    return datetime.now(timezone.utc)


def contributor_expired(receipt: dict | None, *, now=None):
    if not receipt:
        return False
    current = now or utc_now()
    for source in receipt.get("sources", ()):
        response = source.get("contribution")
        if response is None:
            continue
        for fragment in ContextContributorResponse.model_validate(response).fragments:
            if fragment.expires_at is not None and fragment.expires_at <= current:
                return True
    return False
