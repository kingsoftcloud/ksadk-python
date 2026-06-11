from __future__ import annotations

import os
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class ToolPolicy:
    risk_level: str = "low"
    side_effects: Sequence[str] = field(default_factory=tuple)
    requires_approval: bool | None = None


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
        value = os.environ.get("KSADK_TOOL_APPROVAL_MODE", "").strip().lower()
        return value or "off"

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
    mode = (approval_mode or os.environ.get("KSADK_TOOL_APPROVAL_MODE", "")).strip().lower() or "off"
    if policy.requires_approval is not None:
        return policy.requires_approval and mode != "off"
    if mode != "strict":
        return False
    return policy.risk_level.lower() in {"medium", "high", "critical"}


def _canonical_tool_args(tool_args: Any) -> str:
    try:
        return json.dumps(tool_args or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"tool_receipt:{digest}"


def approval_interrupt_info_from_result(
    result: Any,
    *,
    fallback_tool_name: str = "tool",
    tool_args: Any = None,
    run_id: str | None = None,
) -> dict[str, Any] | None:
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
        "side_effects": list(approval_request.get("side_effects") or result.get("side_effects") or []),
        "server_label": str(approval_request.get("server_label") or result.get("server_label") or "ksadk"),
    }
    if run_id:
        interrupt["run_id"] = str(run_id)
    return interrupt
