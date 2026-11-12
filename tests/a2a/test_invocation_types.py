"""Frozen, credential-free contracts for one managed remote invocation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from google.protobuf.json_format import MessageToDict
from pydantic import ValidationError

from ksadk.a2a.control_plane import A2ATarget
from ksadk.a2a.identity import A2AApiKeyPrincipal, canonical_a2a_authn_mode
from ksadk.a2a.invocation_types import (
    A2ACancelResult,
    A2AContinuationRequest,
    A2AInvocationContext,
    A2AInvocationError,
    A2AInvocationHandle,
    A2AInvocationMessage,
    A2AInvocationOutputRef,
    A2AInvocationRequest,
    A2AInvocationSnapshot,
    RemoteAgentSpec,
)

SPACE = "a2a-space-00000000000040008000000000000041"
TARGET = "a2a-agent-00000000000040008000000000000041"
VERSION = "a2a-version-00000000000040008000000000000041"
OTHER_VERSION = "a2a-version-00000000000040008000000000000042"
TASK = "a2a-task-00000000000040008000000000000041"


def spec(**updates: object) -> RemoteAgentSpec:
    values = {
        "local_name": "finance",
        "binding_id": "binding-finance-v1",
        "space_id": SPACE,
        "target_agent_id": TARGET,
        "expected_version_id": VERSION,
    }
    values.update(updates)
    return RemoteAgentSpec.model_validate(values)


def handle(**updates: object) -> A2AInvocationHandle:
    values = {
        "run_id": "run-1",
        "session_id": "session-1",
        "parent_scope_id": "scope-root",
        "call_id": "a2a-call-1",
        "binding_id": "binding-finance-v1",
        "target_agent_id": TARGET,
        "attempt_id": "attempt-1",
    }
    values.update(updates)
    return A2AInvocationHandle.model_validate(values)


def context() -> A2AInvocationContext:
    return A2AInvocationContext(caller_agent_id="ar-root", audit_id="audit-1")


@pytest.mark.parametrize("status,source", [
    ("completed", "local"), ("submitted", "local"),
    ("cancelled", "stream"), ("cancelled", "get_task"),
])
def test_unallocated_task_only_allows_local_cancel_snapshot(status, source):
    with pytest.raises(ValueError, match="A2A_PLATFORM_TASK_REQUIRED"):
        A2AInvocationSnapshot(
            handle=handle(), status=status, source=source,
            observed_at_unix_seconds=1.0,
        )


def test_unallocated_local_cancel_roundtrips_and_can_be_replayed():
    snapshot = A2AInvocationSnapshot(
        handle=handle(), status="cancelled", source="local",
        observed_at_unix_seconds=1.0,
    )
    for outcome in ("local_cancelled_before_send", "already_terminal"):
        result = A2ACancelResult(
            handle=handle(), outcome=outcome, snapshot=snapshot,
            requested_at_unix_seconds=1.0, resolved_at_unix_seconds=1.0,
        )
        assert A2ACancelResult.model_validate_json(result.model_dump_json()) == result


def message(message_id: str = "message-1") -> A2AInvocationMessage:
    return A2AInvocationMessage(message_id=message_id, text_parts=("hello", " world"))


@pytest.mark.parametrize(
    "value",
    ["false", False, 0, None, ["Shanghai"], {"result": "Shanghai"}],
)
def test_invocation_message_preserves_typed_data_parts(value: object) -> None:
    message = A2AInvocationMessage(message_id="typed-input", data_parts=(value,))
    restored = A2AInvocationMessage.model_validate_json(message.model_dump_json())
    assert restored == message
    wire = message.to_wire()
    assert len(wire.parts) == 1
    assert MessageToDict(wire.parts[0].data) == value


def test_invocation_message_requires_at_least_one_nonempty_part() -> None:
    with pytest.raises(ValidationError, match="A2A_MESSAGE_CONTENT_REQUIRED"):
        A2AInvocationMessage(message_id="empty")
    with pytest.raises(ValidationError, match="A2A_MESSAGE_CONTENT_REQUIRED"):
        A2AInvocationMessage(message_id="blank", text_parts=(" ",))


def test_declaration_roundtrip_is_immutable_pinned_and_credential_free() -> None:
    value = spec()
    restored = RemoteAgentSpec.model_validate_json(value.model_dump_json())
    assert restored == value
    assert restored.resolve_version(VERSION) == restored
    assert set(restored.model_dump()) == {
        "local_name",
        "binding_id",
        "space_id",
        "target_agent_id",
        "expected_version_id",
        "description",
        "exposure",
        "timeout_seconds",
    }
    with pytest.raises(ValidationError):
        restored.target_agent_id = TARGET
    with pytest.raises(ValueError, match="VERSION_DRIFT"):
        restored.resolve_version(OTHER_VERSION)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("space_id", "https://example.com/card"),
        ("target_agent_id", "finance"),
        ("expected_version_id", "latest"),
        ("binding_id", " "),
        ("local_name", "finance-agent"),
        ("timeout_seconds", True),
        ("exposure", "expert_tool"),
        ("endpoint", "https://example.com"),
        ("credential", "fake-secret"),
    ],
)
def test_invalid_or_authoritative_duplicate_declaration_fields_are_rejected(
    field: str, value: object
) -> None:
    values = spec().model_dump()
    values[field] = value
    with pytest.raises(ValidationError):
        RemoteAgentSpec.model_validate(values)


def test_production_requires_a_version_pin() -> None:
    floating = spec(expected_version_id=None)
    with pytest.raises(ValueError, match="VERSION_PIN_REQUIRED"):
        floating.resolve_version(VERSION)
    assert floating.resolve_version(VERSION, production=False).expected_version_id == VERSION


def test_handle_scope_is_stable_before_and_after_remote_binding() -> None:
    initial = handle()
    expected_scope = "scope_38058b76c8de66a75b01b778"
    assert initial.scope_id == expected_scope
    platform_bound = initial.with_platform_task(TASK)
    remote_bound = platform_bound.bind_remote_task("remote-task-1", "remote-context-1")
    assert initial.scope_id == platform_bound.scope_id == remote_bound.scope_id
    assert initial.platform_task_id is None
    assert remote_bound.platform_task_id == TASK
    with pytest.raises(ValueError, match="BINDING_CONFLICT"):
        remote_bound.bind_remote_task("remote-task-2")


@pytest.mark.parametrize(
    ("field", "value"),
    [("attempt", 0), ("scope_id", "caller-owned"), ("endpoint", "https://internal")],
)
def test_handle_rejects_legacy_or_derived_identity_inputs(field: str, value: object) -> None:
    values = handle().model_dump()
    values[field] = value
    with pytest.raises(ValidationError):
        A2AInvocationHandle.model_validate(values)


def test_request_owns_message_digest_and_rejects_mismatched_binding() -> None:
    request = A2AInvocationRequest(
        spec=spec(),
        handle=handle(),
        message=message(),
        context=context(),
        deadline_unix_seconds=2_000_000_000.0,
        idempotency_token="idempotency-1",
    )
    assert request.scope_id == handle().scope_id
    assert request.message_sha256 == (
        "edd77f935cf15ed660cf06b8d7f8cb6787d92f7f0db8e501d4e036dff6495af9"
    )
    assert request.message.to_wire().message_id == "message-1"
    with pytest.raises(ValidationError, match="BINDING_MISMATCH"):
        A2AInvocationRequest(
            spec=spec(binding_id="binding-other"),
            handle=handle(),
            message=message(),
            context=context(),
            deadline_unix_seconds=2_000_000_000.0,
            idempotency_token="idempotency-2",
        )


def test_continuation_requires_remote_binding_and_preserves_scope() -> None:
    bound = handle().with_platform_task(TASK).bind_remote_task(
        "remote-task-1", "remote-context-1"
    )
    request = A2AContinuationRequest(
        spec=spec(),
        handle=bound,
        message=message("message-2"),
        context=context(),
        deadline_unix_seconds=2_000_000_001.0,
        idempotency_token="continue-1",
    )
    wire = request.to_wire_message()
    assert request.scope_id == handle().scope_id
    assert wire.task_id == "remote-task-1"
    assert wire.context_id == "remote-context-1"
    with pytest.raises(ValidationError, match="REMOTE_TASK_REQUIRED"):
        A2AContinuationRequest(
            spec=spec(),
            handle=handle().with_platform_task(TASK),
            message=message("message-3"),
            context=context(),
            deadline_unix_seconds=2_000_000_002.0,
            idempotency_token="continue-2",
        )


def test_snapshot_and_cancel_results_never_invent_remote_cancellation() -> None:
    bound = handle().with_platform_task(TASK).bind_remote_task("remote-task-1")
    output = A2AInvocationOutputRef(scope_id=bound.scope_id, item_id="answer")
    completed = A2AInvocationSnapshot(
        handle=bound,
        status="completed",
        source="get_task",
        observed_at_unix_seconds=100.0,
        output_refs=(output,),
    )
    late = A2ACancelResult(
        handle=bound,
        outcome="already_terminal",
        requested_at_unix_seconds=99.0,
        resolved_at_unix_seconds=100.0,
        snapshot=completed,
    )
    unknown = A2ACancelResult(
        handle=bound,
        outcome="cancel_requested_but_remote_unknown",
        requested_at_unix_seconds=99.0,
        resolved_at_unix_seconds=100.0,
    )
    assert late.confirmed_cancelled is False
    assert unknown.confirmed_cancelled is False
    with pytest.raises(ValidationError, match="CANCELLED_SNAPSHOT_REQUIRED"):
        A2ACancelResult(
            handle=bound,
            outcome="remote_cancelled",
            requested_at_unix_seconds=99.0,
            resolved_at_unix_seconds=100.0,
        )


def test_failed_snapshot_requires_sanitized_error_and_scoped_outputs() -> None:
    bound = handle().with_platform_task(TASK)
    with pytest.raises(ValidationError, match="ERROR_REQUIRED"):
        A2AInvocationSnapshot(
            handle=bound,
            status="failed",
            source="local",
            observed_at_unix_seconds=100.0,
        )
    with pytest.raises(ValidationError, match="OUTPUT_SCOPE_MISMATCH"):
        A2AInvocationSnapshot(
            handle=bound,
            status="completed",
            source="local",
            observed_at_unix_seconds=100.0,
            output_refs=(A2AInvocationOutputRef(scope_id="other", item_id="answer"),),
        )
    error = A2AInvocationError(code="A2A_REMOTE_FAILED", message="remote failed")
    assert "traceback" not in error.model_dump_json().lower()


def test_external_principal_and_auth_mode_wire_values_are_frozen() -> None:
    principal = A2AApiKeyPrincipal(
        key_id="key-1",
        account_id="account-1",
        sub_account_id="sub-1",
        principal_id="principal-1",
        product_scopes=frozenset({"A2A", "MCP"}),
        expires_at=datetime(2026, 12, 1, tzinfo=UTC),
    )
    assert principal.has_required_scope()
    assert canonical_a2a_authn_mode("api_key") == "platform_api_key"
    assert canonical_a2a_authn_mode("platform_api_key") == "platform_api_key"
    assert canonical_a2a_authn_mode("console_session") == "console_session"
    assert canonical_a2a_authn_mode("workload_permit") == "workload_permit"
    assert canonical_a2a_authn_mode("gateway_service") == "gateway_service"
    with pytest.raises(ValueError, match="unsupported A2A authentication mode"):
        canonical_a2a_authn_mode("anonymous")
    with pytest.raises(ValidationError):
        A2AApiKeyPrincipal.model_validate(
            {**principal.model_dump(), "product_scopes": ["MCP"]}
        )


def test_control_plane_target_must_match_the_declared_fixed_target() -> None:
    target = A2ATarget(agent_id=TARGET, version_id=VERSION, card_sha256="a" * 64)
    target.assert_matches(target_agent_id=TARGET, expected_version_id=VERSION)
    with pytest.raises(ValueError, match="TARGET_MISMATCH"):
        target.assert_matches(
            target_agent_id="a2a-agent-00000000000040008000000000000042",
            expected_version_id=VERSION,
        )
    with pytest.raises(ValueError, match="VERSION_DRIFT"):
        target.assert_matches(target_agent_id=TARGET, expected_version_id=OTHER_VERSION)
