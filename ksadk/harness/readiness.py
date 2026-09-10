"""Harness 运行就绪度稳定合同。

把不可变 :class:`HarnessSpec` 与一次 smoke/draft Run 的公开事件合并为
``ready / warning / blocked``。该投影只消费公开合同，不解析 Engine、MCP、
Skill 或 Sandbox 私有对象，可直接供生命周期门禁与 Studio 展示。

``unknown`` 不等于故障：渐进披露能力在尚未被模型使用前本来就是 unknown，
因此只产生 warning；只有已观测到的必需能力故障、模型失败或 Run 失败才阻断。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Sequence

from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.observability import capability_health
from ksadk.harness.spec import CapabilityBinding, HarnessSpec

_STATUS_ORDER = {"ready": 0, "warning": 1, "blocked": 2}


def runtime_readiness(
    spec: HarnessSpec,
    events: Sequence[RuntimeEvent] = (),
) -> dict[str, Any]:
    """返回 JSON 兼容的运行/部署就绪度快照。

    该函数不执行探测。``events`` 应来自同一 Revision 的 draft/smoke Run；
    空事件仍可做静态检查，但模型和按需能力会标记为尚未观测。
    """

    projected = capability_health(events)
    health_by_ref = {
        str(item["capability_ref"]): item for item in projected.get("items", ())
    }
    checks: list[dict[str, Any]] = [
        _check(
            check_id="spec",
            category="configuration",
            status="ready",
            reason_code="spec_valid",
            resource_ref=spec.agent_revision_ref,
            required=True,
        ),
        _model_check(spec, events),
    ]
    checks.extend(
        _capability_check("mcp", binding, health_by_ref.get(binding.capability_ref))
        for binding in spec.capabilities.mcp_bindings
    )
    checks.extend(
        _capability_check("skill", binding, health_by_ref.get(binding.capability_ref))
        for binding in spec.capabilities.skill_bindings
    )

    # Sandbox 只有在引擎实际声明/使用时才属于该 Agent 的运行依赖；默认
    # SandboxPolicy 本身不代表 Agent 一定绑定了 Sandbox Tool。
    checks.extend(
        _observed_capability_check(item)
        for item in projected.get("items", ())
        if item.get("kind") == "sandbox"
    )
    checks.append(_approval_check(spec))
    checks.append(_run_check(events))

    counts = Counter(str(item["status"]) for item in checks)
    overall = max((str(item["status"]) for item in checks), key=_STATUS_ORDER.__getitem__)
    run_ids = sorted(
        {
            str(event.run_id or event.invocation_id)
            for event in events
            if event.run_id or event.invocation_id
        }
    )
    return {
        "status": overall,
        "deployable": overall != "blocked",
        "revision_ref": spec.agent_revision_ref,
        "spec_hash": spec.content_hash(),
        "observed_run_ids": run_ids,
        "counts": {name: counts.get(name, 0) for name in ("ready", "warning", "blocked")},
        "checks": checks,
    }


def _model_check(spec: HarnessSpec, events: Sequence[RuntimeEvent]) -> dict[str, Any]:
    observed = [
        event
        for event in events
        if event.event_type
        in {EventType.MODEL_CALL_COMPLETED, EventType.MODEL_CALL_FAILED}
    ]
    if not observed:
        return _check(
            check_id="model",
            category="model",
            status="warning",
            reason_code="not_observed",
            resource_ref=spec.model.profile_ref,
            required=True,
        )
    latest = observed[-1]
    failed = latest.event_type == EventType.MODEL_CALL_FAILED
    check = _check(
        check_id="model",
        category="model",
        status="blocked" if failed else "ready",
        reason_code="model_call_failed" if failed else "model_call_succeeded",
        resource_ref=str(latest.payload.get("model") or spec.model.profile_ref),
        required=True,
    )
    check["configured_resource_ref"] = spec.model.profile_ref
    check["fallback_used"] = bool(latest.payload.get("fallback", False))
    check["attempt"] = int(latest.payload.get("attempt") or 1)
    return check


def _capability_check(
    kind: str,
    binding: CapabilityBinding,
    health: dict[str, Any] | None,
) -> dict[str, Any]:
    observed_status = str((health or {}).get("status") or "unknown")
    if observed_status == "available":
        status, reason = "ready", "capability_available"
    elif observed_status == "degraded":
        status = "blocked" if binding.required else "warning"
        reason = (
            "required_capability_degraded"
            if binding.required
            else "optional_capability_degraded"
        )
    else:
        # on_demand/explicit 以及尚未调用的 always 能力都不能仅因“未使用”判坏。
        status, reason = "warning", "capability_not_observed"
    return _check(
        check_id=f"{kind}:{binding.capability_ref}",
        category=kind,
        status=status,
        reason_code=reason,
        resource_ref=binding.capability_ref,
        required=binding.required,
        load_policy=binding.load_policy,
    )


def _observed_capability_check(item: dict[str, Any]) -> dict[str, Any]:
    required = bool(item.get("required", True))
    observed_status = str(item.get("status") or "unknown")
    if observed_status == "available":
        status, reason = "ready", "capability_available"
    elif observed_status == "degraded":
        status = "blocked" if required else "warning"
        reason = "required_capability_degraded" if required else "optional_capability_degraded"
    else:
        status, reason = "warning", "capability_not_observed"
    capability_ref = str(item.get("capability_ref") or "")
    return _check(
        check_id=f"{item.get('kind', 'capability')}:{capability_ref}",
        category=str(item.get("kind") or "capability"),
        status=status,
        reason_code=reason,
        resource_ref=capability_ref,
        required=required,
        load_policy=str(item.get("load_policy") or "on_demand"),
    )


def _approval_check(spec: HarnessSpec) -> dict[str, Any]:
    policy = spec.approval_policy
    if policy.mode == "never" or policy.approver_roles:
        status, reason = "ready", "approval_policy_configured"
    else:
        # 本地调试可由调用者直接批准，正式平台仍应配置组织审批角色。
        status, reason = "warning", "approval_roles_not_configured"
    return _check(
        check_id="approval-policy",
        category="policy",
        status=status,
        reason_code=reason,
        resource_ref=None,
        required=policy.mode != "never",
    )


def _run_check(events: Sequence[RuntimeEvent]) -> dict[str, Any]:
    terminal = [
        event
        for event in events
        if event.event_type
        in {
            EventType.RUN_COMPLETED,
            EventType.RUN_FAILED,
            EventType.RUN_CANCELED,
            EventType.RUN_INTERRUPTED,
        }
    ]
    if not terminal:
        return _check(
            check_id="smoke-run",
            category="runtime",
            status="warning",
            reason_code="smoke_run_not_observed",
            resource_ref=None,
            required=False,
        )
    latest = terminal[-1]
    if latest.event_type == EventType.RUN_COMPLETED:
        status, reason = "ready", "smoke_run_succeeded"
    elif latest.event_type == EventType.RUN_FAILED:
        status, reason = "blocked", "smoke_run_failed"
    else:
        status, reason = "warning", "smoke_run_incomplete"
    return _check(
        check_id="smoke-run",
        category="runtime",
        status=status,
        reason_code=reason,
        resource_ref=None,
        required=False,
    )


def _check(
    *,
    check_id: str,
    category: str,
    status: str,
    reason_code: str,
    resource_ref: str | None,
    required: bool,
    load_policy: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": check_id,
        "category": category,
        "status": status,
        "reason_code": reason_code,
        "required": required,
    }
    if resource_ref:
        result["resource_ref"] = resource_ref
    if load_policy:
        result["load_policy"] = load_policy
    return result


__all__ = ["runtime_readiness"]
