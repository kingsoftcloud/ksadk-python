"""Preserve Runtime-owned identity and controls across native interruptions."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ksadk.runtime.adapter import StartRequest
from ksadk.runtime.preprocessing import PreparedRuntimeStart
from ksadk.runtime_context import (
    TRUSTED_IDENTITY_METADATA_KEY,
    PlatformInvocationContext,
    session_invocation_context,
)
from ksadk.session_context import SessionContext


def preserve_resume_context(
    run: Any,
    current: Any,
    request: StartRequest | None,
    payload: Any,
    handle: Any,
    runtime_type: str,
) -> None:
    previous = current.__dict__ if current is not None else {}
    original = request or previous.get("_start_request")
    trusted = previous.get("_tag_context")
    prepared = previous.get("_prepared_start")
    if isinstance(prepared, PreparedRuntimeStart):
        trusted = (
            replace(prepared.context, session=trusted.session)
            if isinstance(trusted, PlatformInvocationContext)
            else prepared.context
        )
    if isinstance(original, StartRequest):
        if original.session_id != handle.session_id or any(
            key in handle.native_ref and handle.native_ref[key] != getattr(original, key)
            for key in ("agent_id", "user_id")
        ):
            raise ValueError("resume original request does not match its Runtime identity")
        run.__dict__["_start_request"] = original
        if not isinstance(trusted, PlatformInvocationContext):
            metadata = original.metadata
            snapshot, identity = (
                metadata.get("session_context"),
                metadata.get(TRUSTED_IDENTITY_METADATA_KEY),
            )
            trusted = session_invocation_context(
                snapshot,
                agent_id=original.agent_id or "",
                user_id=original.user_id,
                session_id=original.session_id,
                runner_type=runtime_type,
                identity=identity,
                tool_approval_mode=str(metadata.get("tool_approval_mode") or ""),
            )
    if not isinstance(trusted, PlatformInvocationContext):
        return  # public answers/tags cannot establish an invocation identity
    if trusted.session_id != handle.session_id:
        raise ValueError("resume context does not match its Runtime session")
    if payload is not None and payload.session_context is not None:
        trusted = replace(trusted, session=SessionContext.from_payload(payload.session_context))
    run.__dict__["_tag_context"] = trusted
