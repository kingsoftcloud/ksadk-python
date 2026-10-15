"""One fail-closed approval and audit boundary for native and tool invocation."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Literal

from ksadk.runtime_context import get_current_invocation_context
from ksadk.tools.a2a_policy import A2A_REMOTE_TOOL_POLICY
from ksadk.tools.gateway import tool_policy_requires_approval
from ksadk.a2a.policy_refs import resolve_policy_ref


class A2AApprovalRequired(PermissionError):
    """Typed local approval pause; never infer it from a remote error string."""


@dataclass(frozen=True)
class A2AInvocationPolicyDecision:
    action: Literal["allow", "deny", "approval_required"]
    reason_code: str | None = None


class A2AInvocationPolicy:
    def __init__(self, *, evaluator=None, audit=None):
        self._evaluator, self._audit = evaluator, audit

    async def evaluate(self, request, *, route_kind=None):
        if route_kind not in {None, "hosted_gateway"}:
            decision = A2AInvocationPolicyDecision("deny", "A2A_HOSTED_ROUTE_REQUIRED")
        elif getattr(request, "approval_decision", None) == "denied":
            decision = A2AInvocationPolicyDecision("deny", "A2A_APPROVAL_DENIED")
        elif self._evaluator is None:
            decision = A2AInvocationPolicyDecision("deny", "A2A_POLICY_NOT_CONFIGURED")
        else:
            decision = self._evaluator(request, route_kind)
            if inspect.isawaitable(decision):
                decision = await decision
            if not isinstance(decision, A2AInvocationPolicyDecision):
                raise TypeError("A2A_POLICY_INVALID_DECISION")
            if decision.action not in {"allow", "deny", "approval_required"}:
                raise ValueError("A2A_POLICY_INVALID_DECISION")
        if self._audit:
            receipt = self._audit(request, decision, route_kind)
            if inspect.isawaitable(receipt):
                await receipt
        return decision


def runtime_a2a_invocation_policy(*, audit, approval_resolver=None):
    """Share the runtime tool profile, using only trusted approval resolution.

    The resolver is injected by the runtime interaction owner and must check
    the exact invocation against its durable approval ledger. Public request
    correlation fields and a caller-supplied 'approved' value are not proof.
    Audit is required and awaited before any control-plane or remote request.
    """
    if not callable(audit):
        raise TypeError("A2A_AUDIT_REQUIRED")
    if approval_resolver is not None and not callable(approval_resolver):
        raise TypeError("A2A_APPROVAL_RESOLVER_INVALID")

    async def evaluate(request, _route):
        try:
            profile = resolve_policy_ref(getattr(getattr(request, "spec", None), "policy_ref", None))
        except ValueError:
            return A2AInvocationPolicyDecision("deny", "A2A_POLICY_REF_UNSUPPORTED")
        context = get_current_invocation_context()
        if context is None:
            return A2AInvocationPolicyDecision("deny", "A2A_RUNTIME_CONTEXT_REQUIRED")
        approval = None
        if approval_resolver is not None:
            approval = approval_resolver(request)
            if inspect.isawaitable(approval):
                approval = await approval
            if approval not in {None, "approved", "denied"}:
                raise ValueError("A2A_APPROVAL_RESOLUTION_INVALID")
        if approval == "denied":
            return A2AInvocationPolicyDecision("deny", "A2A_APPROVAL_DENIED")
        if approval == "approved" or (profile == "runtime" and not tool_policy_requires_approval(
            A2A_REMOTE_TOOL_POLICY, approval_mode=context.tool_approval_mode
        )):
            return A2AInvocationPolicyDecision("allow")
        return A2AInvocationPolicyDecision("approval_required", "A2A_APPROVAL_REQUIRED")

    return A2AInvocationPolicy(evaluator=evaluate, audit=audit)
