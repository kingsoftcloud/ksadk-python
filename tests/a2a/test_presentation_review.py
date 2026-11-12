"""Public boundary regressions from the fixed R85-1 review."""

import json

import pytest
from pydantic import ValidationError

from ksadk.a2a.presentation import (
    ExecutionScopeDescriptor,
    ToolActivity,
    build_public_tool_activity,
    validate_scope_transition,
)
from tests.a2a.test_remote_agent_fixture import FIXTURE


def payload(result):
    return dict(
        event_id="e",
        item_id="i",
        call_id="c",
        item_kind="tool_result",
        operation="completed",
        execution_status="completed",
        result=result,
    )


@pytest.mark.parametrize(
    "key", ["SecretAccessKey", "accessToken", "APIKey", "clientSecret", "secret_access_key"]
)
def test_sensitive_keys_are_redacted_and_direct_public_input_rejected(key):
    raw = {"nested": [{key: "fake-review-value"}]}
    assert "fake-review-value" not in build_public_tool_activity(payload(raw)).model_dump_json()
    with pytest.raises(ValidationError, match="unsafe public"):
        ToolActivity.model_validate(payload(raw))


@pytest.mark.parametrize(
    "key", ["TraceBack", "trace_back", "trace-back", "stackTrace", "stack_trace"]
)
def test_stack_trace_keys_are_redacted_after_case_normalization(key):
    raw = {"nested": [{key: "private stack details"}]}
    public = build_public_tool_activity(payload(raw))

    assert "private stack details" not in public.model_dump_json()
    assert public.truncated is True
    with pytest.raises(ValidationError, match="unsafe public"):
        ToolActivity.model_validate(payload(raw))


@pytest.mark.parametrize(
    "url",
    [
        "http://[::1]/admin",
        "http://[fd00::1]:8080/x",
        "https://[fe80::1%25eth0]/x",
        "http://[::ffff:127.0.0.1]/x",
        "http://10.0.0.1/x",
    ],
)
def test_internal_urls_are_redacted_and_direct_public_input_rejected(url):
    raw = {"nested": [f"result: {url}"]}
    assert url not in build_public_tool_activity(payload(raw)).model_dump_json()
    with pytest.raises(ValidationError, match="unsafe public"):
        ToolActivity.model_validate(payload(raw))


def descriptor():
    return next(
        json.loads(line)["payload"]
        for line in (FIXTURE / "a2a_stream_tool_terminal.jsonl").read_text().splitlines()
        if json.loads(line)["kind"] == "scope_descriptor"
    )


@pytest.mark.parametrize("capability", ["unknown", "unsupported", "supported"])
def test_pre_send_local_confirmation_is_independent_of_remote_capability(capability):
    before = ExecutionScopeDescriptor.model_validate(descriptor())
    after = ExecutionScopeDescriptor.model_validate(
        {
            **before.model_dump(),
            "status": "cancelled",
            "ended_at": before.started_at + 1,
            "cancel": {"capability": capability, "request_state": "local_confirmed"},
        }
    )
    validate_scope_transition(before, after)
    assert ExecutionScopeDescriptor.model_validate_json(after.model_dump_json()) == after
    working = before.model_copy(update={"status": "working"})
    with pytest.raises(ValueError, match="LOCAL_CANCEL"):
        validate_scope_transition(working, after)


@pytest.mark.parametrize("capability", ["unknown", "unsupported"])
def test_remote_confirmation_still_requires_supported(capability):
    before = descriptor()
    with pytest.raises(ValidationError, match="CAPABILITY_MISMATCH"):
        ExecutionScopeDescriptor.model_validate(
            {
                **before,
                "status": "cancelled",
                "ended_at": before["started_at"] + 1,
                "cancel": {"capability": capability, "request_state": "confirmed"},
            }
        )
