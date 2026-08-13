from __future__ import annotations

import hashlib
import json
import os
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from ksadk.runtime_context import get_current_invocation_context


@dataclass(frozen=True)
class ToolPolicy:
    risk_level: str = "low"
    side_effects: Sequence[str] = field(default_factory=tuple)
    # Approval scopes classify operations which might need an interactive
    # decision. ``public_network`` is intentionally a read-only exception:
    # web discovery must remain available in every approval profile.
    approval_scopes: Sequence[str] = field(default_factory=tuple)
    # Use only for tools whose operation is inherently safe to run without a
    # human decision. This wins over a risk level or explicit legacy policy.
    approval_exempt: bool = False
    requires_approval: bool | None = None


_READ_ONLY_GIT_COMMANDS = {"status", "diff", "log", "show", "branch", "ls-files"}
_DANGEROUS_COMMAND_TOKENS = {
    "sudo",
    "kubectl",
    "docker",
}
_DANGEROUS_GIT_COMMANDS = {"reset", "clean", "push", "checkout"}


class ToolGateway:
    def __init__(self, policies: Mapping[str, ToolPolicy] | None = None):
        self._policies = dict(policies or {})

    def invoke(
        self,
        tool_name: str,
        func: Callable[..., Any],
        *args: Any,
        approval: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        policy = self._policies.get(tool_name, ToolPolicy())
        if self._is_rejected(approval):
            return self._approval_rejected(tool_name, policy, approval)
        if self._is_approved(approval) or not self._requires_approval(policy):
            return func(*args, **kwargs)
        return self._approval_required(tool_name, policy)

    def _requires_approval(self, policy: ToolPolicy) -> bool:
        return tool_policy_requires_approval(policy, approval_mode=self._approval_mode())

    @staticmethod
    def _approval_mode() -> str:
        context = get_current_invocation_context()
        requested_mode = context.tool_approval_mode if context is not None else None
        return normalize_tool_approval_mode(requested_mode)

    @staticmethod
    def _is_approved(approval: Mapping[str, Any] | None) -> bool:
        if not approval:
            return False
        return bool(approval.get("approved") or approval.get("approve"))

    @staticmethod
    def _is_rejected(approval: Mapping[str, Any] | None) -> bool:
        if not approval:
            return False
        if "approved" in approval:
            return not bool(approval.get("approved"))
        if "approve" in approval:
            return not bool(approval.get("approve"))
        return False

    @staticmethod
    def _approval_required(tool_name: str, policy: ToolPolicy) -> dict[str, Any]:
        return {
            "ok": False,
            "type": "approval_required",
            "approval_required": True,
            "approval_request": {
                "id": f"appr_{uuid4().hex}",
                "tool_name": tool_name,
                "risk_level": policy.risk_level,
                "side_effects": list(policy.side_effects),
            },
        }

    @staticmethod
    def _approval_rejected(
        tool_name: str,
        policy: ToolPolicy,
        approval: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "type": "approval_rejected",
            "approval_required": False,
            "tool_name": tool_name,
            "risk_level": policy.risk_level,
            "reason": str((approval or {}).get("reason") or ""),
        }


def default_tool_gateway(policies: Mapping[str, ToolPolicy] | None = None) -> ToolGateway:
    return ToolGateway(policies)


def tool_policy_requires_approval(
    policy: ToolPolicy,
    *,
    approval_mode: str | None = None,
) -> bool:
    mode = normalize_tool_approval_mode(approval_mode)
    if policy.approval_exempt or _is_read_only_public_network_policy(policy):
        return False
    if policy.requires_approval is not None:
        return policy.requires_approval and mode != "full"
    if mode == "full":
        return False
    if mode == "ask" and policy.approval_scopes:
        return True
    return policy.risk_level.lower() in {"medium", "high", "critical"}


def _is_read_only_public_network_policy(policy: ToolPolicy) -> bool:
    """Keep public web reads available even under the most cautious profile.

    A network write must declare a side effect, so it does not match this
    exemption and can still require approval.
    """
    return bool(policy.approval_scopes) and set(policy.approval_scopes) <= {
        "public_network"
    } and not policy.side_effects


def normalize_tool_approval_mode(value: str | None = None) -> str:
    """Resolve the compact runtime approval profile.

    ``ask`` confirms risky and non-network scoped operations; ``risk``
    confirms medium-and-higher risk only; ``full`` leaves default policies
    unprompted. Public web reads remain approval-free in every profile. The
    process environment remains a default for non-UI callers, while a
    request-scoped mode wins for the duration of that invocation.
    """
    raw = str(value or os.environ.get("KSADK_TOOL_APPROVAL_MODE", "risk")).strip().lower()
    return raw if raw in {"ask", "risk", "full"} else "risk"


def tool_approval_capability() -> dict[str, Any]:
    """Describe the single profile interface exposed to hosted UIs."""
    return {
        "Modes": ["ask", "risk", "full"],
        "DefaultMode": normalize_tool_approval_mode(),
        "RuntimeOverride": True,
    }


def check_command_policy(command: str) -> dict[str, Any]:
    text = str(command or "").strip()
    if not text:
        return {
            "ok": False,
            "decision": "reject",
            "error_type": "command_required",
            "error_message": "command is required",
        }
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        return {
            "ok": False,
            "decision": "reject",
            "error_type": "invalid_command",
            "error_message": str(exc),
        }
    if not tokens:
        return {
            "ok": False,
            "decision": "reject",
            "error_type": "command_required",
            "error_message": "command is required",
        }
    if _contains_recursive_rm(tokens):
        return _command_rejected("recursive rm is not allowed without explicit approval")
    if any(token in _DANGEROUS_COMMAND_TOKENS for token in tokens):
        return _command_rejected(f"{tokens[0]} command is not allowed by default policy")
    if tokens[0] == "git" and len(tokens) > 1:
        subcommand = tokens[1]
        if subcommand in _READ_ONLY_GIT_COMMANDS:
            return {"ok": True, "decision": "allow", "reason": "read_only_git"}
        if subcommand in _DANGEROUS_GIT_COMMANDS:
            return _command_rejected(f"git {subcommand} is not allowed without explicit approval")
    if _references_metadata_endpoint(tokens):
        return _command_rejected(
            "metadata/private endpoint access is not allowed by default policy"
        )
    return {"ok": True, "decision": "allow", "reason": "default_allow"}


def _command_rejected(message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "decision": "reject",
        "error_type": "command_rejected",
        "error_message": message,
    }


def _contains_recursive_rm(tokens: Sequence[str]) -> bool:
    for index, token in enumerate(tokens[:-1]):
        if token == "rm" and any(
            flag.startswith("-") and ("r" in flag or "R" in flag)
            for flag in tokens[index + 1 : index + 3]
        ):
            return True
    return False


def _references_metadata_endpoint(tokens: Sequence[str]) -> bool:
    joined = " ".join(tokens)
    return "169.254.169.254" in joined or "metadata.google.internal" in joined


def _canonical_tool_args(tool_args: Any) -> str:
    try:
        return json.dumps(
            tool_args or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except TypeError:
        return json.dumps(str(tool_args), ensure_ascii=False, separators=(",", ":"))


def build_tool_receipt_idempotency_key(
    *,
    session_id: str,
    run_id: str,
    checkpoint_id: str | None = None,
    tool_call_id: str | None = None,
    tool_name: str,
    tool_args: Any = None,
) -> str:
    payload = {
        "session_id": str(session_id or ""),
        "run_id": str(run_id or ""),
        "checkpoint_id": str(checkpoint_id or ""),
        "tool_call_id": str(tool_call_id or ""),
        "tool_name": str(tool_name or ""),
        "tool_args": _canonical_tool_args(tool_args),
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"tool_receipt:{digest}"


def approval_interrupt_info_from_result(
    result: Any,
    *,
    fallback_tool_name: str = "tool",
    tool_args: Any = None,
    run_id: str | None = None,
) -> dict[str, Any] | None:
    # LangGraph exposes ToolMessage.content as text in callback and graph-update
    # streams.  Preserve the structured gateway contract when that text is a
    # JSON object so an approval remains an interrupt instead of becoming a
    # normal tool result followed by model-authored prose.
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (TypeError, ValueError):
            return None
    if not isinstance(result, Mapping):
        return None
    if str(result.get("type") or "") != "approval_required":
        return None
    approval_request = result.get("approval_request")
    if not isinstance(approval_request, Mapping):
        return None

    request_id = str(
        approval_request.get("approval_request_id")
        or approval_request.get("id")
        or f"appr_{uuid4().hex}"
    )
    interrupt = {
        "id": request_id,
        "approval_request_id": request_id,
        "tool_name": str(approval_request.get("tool_name") or fallback_tool_name or "tool"),
        "arguments": (
            approval_request.get("arguments")
            or approval_request.get("tool_args")
            or approval_request.get("args")
            or tool_args
            or {}
        ),
        "risk_level": str(approval_request.get("risk_level") or result.get("risk_level") or ""),
        "side_effects": list(
            approval_request.get("side_effects") or result.get("side_effects") or []
        ),
        "server_label": str(
            approval_request.get("server_label") or result.get("server_label") or "ksadk"
        ),
    }
    if run_id:
        interrupt["run_id"] = str(run_id)
    return interrupt
