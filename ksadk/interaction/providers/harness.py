"""Managed Harness interactions resume the original durable thread checkpoint."""

from __future__ import annotations

from ksadk.interaction.contracts import InteractionRecord, InteractionSubmission
from ksadk.interaction.provider import InteractionResolveContext, require_capability
from ksadk.runtime.adapter import CancelResult, ResumePayload, ResumeTarget, RunHandle


class HarnessInteractionProvider:
    provider_id = "harness"
    mode = "durable_resume"

    async def resolve(
        self,
        context: InteractionResolveContext,
        record: InteractionRecord,
        submission: InteractionSubmission,
    ) -> RunHandle:
        require_capability(context, "resume", provider_id=self.provider_id)
        require_capability(context, "submit_interaction", provider_id=self.provider_id)
        native_target = record.native_target or {}
        thread_id = str(native_target.get("thread_id") or "")
        if not thread_id or thread_id != context.handle.native_ref.get("thread_id"):
            raise ValueError("Harness interaction must target its original checkpoint thread")
        if submission.action == "cancel":
            require_capability(context, "cancel", provider_id=self.provider_id)
            result = await context.adapter.cancel(context.handle)
            if result == CancelResult.FAILED:
                raise RuntimeError("Harness could not cancel the pending interaction")
            return context.handle
        response = submission.response
        if record.kind == "approval":
            if submission.action in {"approve", "reject"}:
                response = {"decision": submission.action}
            if not isinstance(response, dict) or response.get("decision") not in {
                "approve",
                "reject",
            }:
                raise ValueError("Harness approval requires an explicit approve or reject decision")
        return await context.adapter.resume(
            context.handle,
            ResumeTarget(kind="thread_id", id=thread_id),
            ResumePayload(
                kind="approval_decision" if record.kind == "approval" else "hitl_answer",
                call_id=str(native_target.get("call_id") or record.interaction_id),
                data=response,
                session_context=context.session_context,
            ),
        )
