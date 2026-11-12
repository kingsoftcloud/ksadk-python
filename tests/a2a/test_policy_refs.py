"""Frozen policy references are executable constraints, not display-only labels."""
import json
from types import SimpleNamespace

import pytest

from ksadk.a2a.invocation_policy import runtime_a2a_invocation_policy
from ksadk.runtime_context import (
    reset_current_invocation_context, session_invocation_context, set_current_invocation_context,
)
from tests.a2a.test_invocation_service_r85 import request


@pytest.mark.parametrize("ref,approval,expected", [
    (None, None, "allow"),
    ("policy://a2a/runtime", None, "allow"),
    ("policy://a2a/strict", None, "approval_required"),
    ("policy://a2a/strict", "approved", "allow"),
    ("policy://a2a/strict", "denied", "deny"),
    ("policy://missing-policy", None, "deny"),
    ("policy://missing-policy", "approved", "deny"),
])
async def test_reference_is_enforced_even_with_full_session_profile(ref, approval, expected):
    decisions = []
    async def audit(req, decision, route):
        decisions.append(decision)
    policy = runtime_a2a_invocation_policy(audit=audit, approval_resolver=lambda req: approval)
    context = session_invocation_context({}, agent_id="caller", session_id="s")
    context.tool_approval_mode = "full"
    token = set_current_invocation_context(context)
    try:
        # Defensive runtime check also covers a malformed/bypassed declaration.
        req = SimpleNamespace(spec=SimpleNamespace(policy_ref=ref), approval_decision="approved")
        decision = await policy.evaluate(req, route_kind="hosted_gateway")
        assert decision.action == expected
        assert decisions == [decision]
    finally:
        reset_current_invocation_context(token)


def test_absent_policy_ref_preserves_legacy_nested_checkpoint_serialization():
    req = request()
    assert "policy_ref" not in req.spec.model_dump()
    assert "policy_ref" not in req.model_dump()["spec"]
    assert "policy_ref" not in json.loads(req.model_dump_json())["spec"]
    assert type(req).model_validate_json(req.model_dump_json()) == req


@pytest.mark.parametrize("approval", [None, "approved", "denied"])
async def test_strict_policy_stops_outbound_until_trusted_approval(approval):
    from tests.a2a.test_invocation_service_r85 import service, trigger
    async def audit(*args):
        pass
    svc, _, wire = await service(policy=runtime_a2a_invocation_policy(
        audit=audit, approval_resolver=lambda req: approval))
    req = request()
    req = req.model_copy(update={"spec": req.spec.model_copy(update={"policy_ref": "policy://a2a/strict"})})
    context = session_invocation_context({}, agent_id="caller", session_id="s")
    context.tool_approval_mode = "full"
    token = set_current_invocation_context(context)
    try:
        if approval == "approved":
            assert [event async for event in svc.invoke(req, trigger_ref=trigger())]
            assert any(kind == "send" for kind, _ in wire.calls)
        else:
            with pytest.raises(PermissionError, match="A2A_APPROVAL_"):
                _ = [event async for event in svc.invoke(req, trigger_ref=trigger())]
            assert not wire.calls
    finally:
        reset_current_invocation_context(token)
