"""Canonical Studio interaction admission under the owning Run's decision lock."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from ksadk.runtime import ResumePayload
from ksadk.studio.contracts import RunStatus
from ksadk.studio.errors import StudioError

if TYPE_CHECKING:
    from ksadk.studio.run_service import StudioRunService


async def submit_interaction_locked(
    self: StudioRunService,
    run_id: str,
    interaction_id: str,
    *,
    name: str,
    data: dict[str, Any],
    expected_revision: int,
    idempotency_key: str,
    request_digest: str,
    revision_of: Callable[[Mapping[str, Any]], int],
    submit_timeout_seconds: float,
) -> dict[str, Any]:
    events = self.event_store.events(run_id)
    prior = next(
        (
            event
            for event in reversed(events)
            if event.type == "a2ui.action"
            and str(event.data.get("interactionId") or "") == interaction_id
            and revision_of(event.data) > 0
        ),
        None,
    )
    if prior is not None:
        if str(prior.data.get("idempotencyKey") or "") != idempotency_key:
            raise StudioError(
                "INTERACTION_ALREADY_RESOLVED",
                "该交互已由其他请求处理",
                status_code=409,
            )
        if str(prior.data.get("requestDigest") or "") != request_digest:
            raise StudioError(
                "INTERACTION_IDEMPOTENCY_CONFLICT",
                "同一幂等键不能提交不同内容",
                status_code=409,
            )
        # A receipt replay is read-only. The run may already be waiting
        # for a different interaction; never clear that newer wait state.
        return dict(prior.data.get("receipt") or {})

    interaction = next(
        (
            event
            for event in reversed(events)
            if event.type == "a2ui.interaction"
            and str(event.data.get("interactionId") or event.data.get("interaction_id") or "")
            == interaction_id
        ),
        None,
    )
    if interaction is None:
        raise StudioError("INTERACTION_NOT_FOUND", "交互请求不存在", status_code=404)
    revision = revision_of(interaction.data)
    if revision < 1:
        raise StudioError(
            "INTERACTION_READ_ONLY",
            "交互请求缺少权威 revision，只能查看不能操作",
            status_code=409,
        )
    if expected_revision != revision:
        raise StudioError(
            "INTERACTION_REVISION_MISMATCH",
            "交互版本已变化，请刷新后重试",
            status_code=409,
            details={"expectedRevision": expected_revision, "actualRevision": revision},
        )
    recovery_ready = self._native_recovery_ready.get(run_id)
    if recovery_ready is not None:
        # A persisted card can be visible before the crashed owner's lease
        # expires. Wait for the actual replayed interaction route; this is
        # not an approval queue or an acceptance receipt. In particular,
        # the runtime acknowledgement timeout starts only after recovery.
        await recovery_ready.wait()
        if self._detaching_recoveries or self._cancel_flags.get(run_id):
            raise StudioError(
                "INTERACTION_EXPIRED", "运行时交互已失效", status_code=409
            )
        # Replay/failure may change status or revision while we wait. The
        # run lock still serializes decisions, and cancelled HTTP requests
        # leave both the recovery task and approval ledger untouched.
        return await self._submit_interaction_locked(
            run_id, interaction_id, name=name, data=data,
            expected_revision=expected_revision, idempotency_key=idempotency_key,
            request_digest=request_digest,
        )
    record = self.event_store.get(run_id)
    if record.status != RunStatus.WAITING_INPUT:
        raise StudioError(
            "INTERACTION_NOT_PENDING",
            "该 Run 当前没有等待中的交互",
            status_code=409,
        )
    kind = str(interaction.data.get("kind") or "form")
    if kind != "approval" and name not in {"cancel", "skip"}:
        schema = interaction.data.get("inputSchema") or {}
        errors = list(Draft202012Validator(schema).iter_errors(data))
        if errors:
            raise StudioError(
                "INTERACTION_RESPONSE_INVALID",
                "请填写必填问题后再提交。",
                status_code=422,
            )
    provider_call_id = str(
        interaction.data.get("callId") or interaction.data.get("call_id") or ""
    )
    if kind == "approval" and not provider_call_id:
        approval_request = next(
            (
                event
                for event in reversed(events)
                if event.type == "approval.requested"
                and str(event.data.get("approvalId") or event.data.get("interactionId") or "")
                == interaction_id
            ),
            None,
        )
        if approval_request is not None:
            provider_call_id = str(
                approval_request.data.get("callId")
                or approval_request.data.get("call_id")
                or ""
            )
    provider_call_id = provider_call_id or interaction_id
    submitted = self._submitted_interactions.setdefault(run_id, set())
    submitted.update({interaction_id, provider_call_id})
    decision = str(data.get("decision") or name)
    if kind == "approval" and record.runtime_type == "codex":
        # The shared Web action is named `reject`; Codex app-server calls
        # the same decision `deny`/`decline`. Do not let a UI alias fall
        # through as an unsupported native approval decision.
        decision = {
            "reject": "deny",
            "rejected": "deny",
        }.get(decision.lower(), decision)
    payload_data = {**data, "decision": decision}
    payload = ResumePayload(
        kind="approval_decision" if kind == "approval" else "hitl_answer",
        call_id=provider_call_id,
        data=payload_data,
    )
    mode = self._waiting_modes.get(run_id)
    handle = self._active_handles.get(run_id)
    if mode == "live":
        if handle is None or not self.executor.is_attached(handle):
            raise StudioError("INTERACTION_EXPIRED", "运行时交互已失效", status_code=409)
        try:
            await asyncio.wait_for(
                self.executor.submit(handle, payload),
                timeout=submit_timeout_seconds,
            )
        except TimeoutError as exc:
            submitted.difference_update({interaction_id, provider_call_id})
            raise StudioError(
                "INTERACTION_SUBMIT_FAILED",
                "运行时未及时确认交互提交，请重试",
                status_code=409,
            ) from exc
        except Exception as exc:  # noqa: BLE001 - provider errors are user-safe here
            submitted.difference_update({interaction_id, provider_call_id})
            raise StudioError(
                "INTERACTION_SUBMIT_FAILED",
                str(exc),
                status_code=409,
            ) from exc
    elif mode == "resume":
        queue = self._control_queues.get(run_id)
        if queue is None:
            raise StudioError("INTERACTION_EXPIRED", "运行时交互已失效", status_code=409)
        submit_ack = asyncio.get_running_loop().create_future()
        queue.put_nowait(("resume", payload, submit_ack))
        try:
            await asyncio.wait_for(
                submit_ack,
                timeout=submit_timeout_seconds,
            )
        except TimeoutError as exc:
            submitted.difference_update({interaction_id, provider_call_id})
            raise StudioError(
                "INTERACTION_SUBMIT_FAILED",
                "运行时未及时确认交互提交，请重试",
                status_code=409,
            ) from exc
        except Exception as exc:  # noqa: BLE001 - provider errors are user-safe here
            submitted.difference_update({interaction_id, provider_call_id})
            raise StudioError(
                "INTERACTION_SUBMIT_FAILED",
                str(exc),
                status_code=409,
            ) from exc
    else:
        submitted.difference_update({interaction_id, provider_call_id})
        raise StudioError("INTERACTION_EXPIRED", "运行时交互已失效", status_code=409)

    next_revision = revision + 1
    normalized_action = name.lower()
    if normalized_action in {"approve", "approved", "accept", "accepted"}:
        outcome = "approved"
        response_summary = "已同意"
    elif normalized_action in {"reject", "rejected", "deny", "denied", "decline"}:
        outcome = "rejected"
        response_summary = "已拒绝"
    elif normalized_action in {"cancel", "cancelled", "canceled"}:
        outcome = "cancelled"
        response_summary = "已反馈给 Agent" if data.get("feedback") else "已取消"
    else:
        outcome = "submitted"
        response_summary = "已提交"
    resolved_data = {
        "runId": run_id,
        "interactionId": interaction_id,
        "callId": provider_call_id,
        "name": name,
        "action": name,
        "outcome": outcome,
        "actor": "user",
        "responseSummary": response_summary,
        "data": data,
        "revision": next_revision,
        "expectedRevision": expected_revision,
        "idempotencyKey": idempotency_key,
    }
    action_data = {
        "runId": run_id,
        "surfaceId": str(
            interaction.data.get("surfaceId") or interaction.data.get("surface_id") or ""
        ),
        "interactionId": interaction_id,
        "callId": provider_call_id,
        "actionId": f"action-{interaction_id}",
        "name": name,
        "data": data,
        "revision": next_revision,
        "expectedRevision": expected_revision,
        "idempotencyKey": idempotency_key,
        "requestDigest": request_digest,
    }
    _, _, receipt = self.event_store.append_interaction_resolution(
        run_id,
        resolved_type=("approval.resolved" if kind == "approval" else "interaction.resolved"),
        resolved_data=resolved_data,
        action_data=action_data,
    )
    record.status = RunStatus.RUNNING
    self.event_store.save(record)
    self._waiting_modes.pop(run_id, None)
    return receipt
