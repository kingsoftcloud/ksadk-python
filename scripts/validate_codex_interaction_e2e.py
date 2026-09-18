#!/usr/bin/env python3
"""Validate that a real cloud Codex turn emits Interaction/v1 approval.

The validator drives the public AgentEngine Action API with the configured
AWS-V4 credentials, waits for a durable ``interaction.requested`` event, and
always deletes the temporary session.  Output is deliberately limited to
trace identifiers and event types so credentials and model content never enter
release evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from typing import Any

import requests

from ksadk.api.client import AgentEngineClient

DEFAULT_PROMPT = (
    "请使用 run_command 工具执行命令 echo kernel-interaction-gate；"
    "需要批准时停下来等待，不要把工具调用写成普通文本。"
)


def _event_type(event: dict[str, Any]) -> str:
    return str(
        event.get("EventType")
        or event.get("event_type")
        or event.get("Type")
        or event.get("type")
        or ""
    )


def _event_seq(event: dict[str, Any]) -> int | None:
    value = event.get("SeqId") or event.get("seq_id") or event.get("seq")
    return int(value) if value is not None else None


def _event_id(event: dict[str, Any]) -> str | None:
    value = event.get("EventId") or event.get("event_id")
    return str(value) if value else None


def _diagnostic_shape(value: Any, *, depth: int = 0) -> Any:
    """Expose event structure without leaking prompts, arguments or output."""

    if depth >= 3:
        return type(value).__name__
    if isinstance(value, dict):
        shaped: dict[str, Any] = {}
        for key, child in value.items():
            normalized = str(key).replace("_", "").lower()
            if any(
                token in normalized
                for token in ("argument", "input", "output", "text", "content", "reason")
            ):
                shaped[str(key)] = "<redacted>"
            elif normalized in {
                "name",
                "toolname",
                "status",
                "phase",
                "code",
                "type",
                "kind",
                "itemid",
                "callid",
            } and isinstance(child, (str, int, float, bool, type(None))):
                shaped[str(key)] = child
            else:
                shaped[str(key)] = _diagnostic_shape(child, depth=depth + 1)
        return shaped
    if isinstance(value, list):
        return [_diagnostic_shape(child, depth=depth + 1) for child in value[:3]]
    return type(value).__name__


def _tool_event_summary(event: dict[str, Any]) -> dict[str, Any]:
    content = (
        event.get("Content")
        or event.get("content")
        or event.get("Payload")
        or event.get("payload")
        or event.get("Data")
        or event.get("data")
        or {}
    )
    if not isinstance(content, dict):
        content = {}
    item = content.get("item") if isinstance(content.get("item"), dict) else content
    error = item.get("error") if isinstance(item.get("error"), dict) else {}
    return {
        "event_id": _event_id(event),
        "seq_id": _event_seq(event),
        "event_keys": sorted(str(key) for key in event),
        "content_keys": sorted(str(key) for key in content),
        "item_keys": sorted(str(key) for key in item),
        "name": item.get("name") or item.get("tool_name") or item.get("toolName"),
        "status": item.get("status") or content.get("status"),
        "error_code": error.get("code") or item.get("error_code"),
        "metadata_shape": _diagnostic_shape(event.get("Metadata")),
        "state_delta_shape": _diagnostic_shape(event.get("StateDelta")),
    }


def _interaction_request(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = _event_type(event)
    if event_type == "interaction.requested":
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
        interaction_id = str(
            payload.get("interaction_id") or payload.get("InteractionId") or ""
        ).strip()
        if not interaction_id:
            return None
        return {
            "interaction_id": interaction_id,
            "run_id": str(
                payload.get("run_id")
                or event.get("run_id")
                or event.get("InvocationId")
                or ""
            ),
            "revision": int(payload.get("revision") or 1),
            "event_id": _event_id(event),
            "seq_id": _event_seq(event),
            "projection": "canonical",
        }
    if event_type != "approval_request":
        return None
    metadata = event.get("Metadata") or event.get("metadata") or {}
    interrupt = metadata.get("interrupt_info") if isinstance(metadata, dict) else {}
    if not isinstance(interrupt, dict):
        interrupt = {}
    interaction_id = str(
        interrupt.get("approval_request_id") or interrupt.get("id") or ""
    ).strip()
    if not interaction_id:
        return None
    return {
        "interaction_id": interaction_id,
        "run_id": str(event.get("InvocationId") or event.get("invocation_id") or ""),
        "revision": 1,
        "event_id": _event_id(event),
        "seq_id": _event_seq(event),
        "projection": "legacy_session_event",
    }


def _interaction_response(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = _event_type(event)
    if event_type == "interaction.resolved":
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
        interaction_id = str(
            payload.get("interaction_id") or payload.get("InteractionId") or ""
        ).strip()
        if not interaction_id:
            return None
        return {
            "interaction_id": interaction_id,
            "event_id": _event_id(event),
            "seq_id": _event_seq(event),
            "projection": "canonical",
        }
    if event_type != "approval_response":
        return None
    metadata = event.get("Metadata") or event.get("metadata") or {}
    resume_input = metadata.get("resume_input") if isinstance(metadata, dict) else {}
    if not isinstance(resume_input, dict):
        resume_input = {}
    interaction_id = str(
        resume_input.get("approval_request_id") or resume_input.get("id") or ""
    ).strip()
    if not interaction_id:
        return None
    return {
        "interaction_id": interaction_id,
        "event_id": _event_id(event),
        "seq_id": _event_seq(event),
        "projection": "legacy_session_event",
    }


def _terminal_after(events: list[dict[str, Any]], seq_id: int | None) -> bool:
    floor = int(seq_id or 0)
    for event in events:
        if int(_event_seq(event) or 0) <= floor:
            continue
        event_type = _event_type(event)
        if event_type in {"run.completed", "run.failed", "run.cancelled"}:
            return True
        if event_type == "run_status" and str(
            (event.get("Content") or event.get("content") or {}).get("status")
        ) in {"completed", "failed", "cancelled"}:
            return True
    return False


def _receipt_id(receipt: dict[str, Any]) -> str | None:
    value = receipt.get("message_id") or receipt.get("MessageId")
    return str(value) if value else None


def _session_id(created: dict[str, Any]) -> str:
    session = created.get("session") or created.get("Session") or created
    value = (
        session.get("session_id")
        or session.get("SessionId")
        or session.get("id")
        or session.get("Id")
    )
    if not value:
        raise RuntimeError("CreateSession response is missing a session identifier")
    return str(value)


def _read_runtime_events(
    *, endpoint: str, api_key: str, agent_id: str, session_id: str
) -> list[dict[str, Any]]:
    """Read a bounded snapshot from the canonical AgentKernel SSE stream.

    ``ListSessionEvents`` is the compatibility/UI projection and may lag the
    Interaction/v1 ledger after an approval response.  Release evidence must
    therefore use ``SubscribeSessionEvents``: it carries the authoritative
    control/interaction/runtime families through Gateway -> Server admission.
    The endpoint is intentionally long-lived, so a short read timeout closes
    each polling snapshot after all currently buffered frames have arrived.
    """

    response = requests.get(
        endpoint.rstrip("/") + "/agentengine/api/v1/SubscribeSessionEvents",
        headers={
            "Authorization": f"Bearer {api_key}",
        },
        params={"AgentId": agent_id, "SessionId": session_id, "AfterSeq": 0},
        stream=True,
        timeout=(30, 1),
    )
    response.raise_for_status()
    events: list[dict[str, Any]] = []
    try:
        for line in response.iter_lines(chunk_size=1, decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            payload = json.loads(line[6:])
            if isinstance(payload, dict):
                events.append(payload)
    except requests.exceptions.RequestException:
        # A read timeout is the normal snapshot boundary for a long-lived SSE
        # response.  A failure before any frame arrived is a real gate error.
        if not events:
            raise
    finally:
        response.close()
    return events


async def _run(args: argparse.Namespace) -> int:
    client = AgentEngineClient(region=args.region, timeout=args.request_timeout)
    detail = await client.get_agent(args.agent_id, include_api_key=True)
    runtime_access = client._extract_runtime_access(detail)
    endpoint = str(runtime_access.get("endpoint") or "").strip()
    api_key = str(runtime_access.get("api_key") or "").strip()
    if not endpoint or not api_key:
        raise RuntimeError("Agent runtime access is not ready for direct event verification")
    session_id: str | None = None
    started = time.monotonic()
    events: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    receipt: dict[str, Any] = {}
    requested: dict[str, Any] | None = None
    resolved: dict[str, Any] | None = None
    submission: dict[str, Any] = {}
    duplicate: dict[str, Any] = {}
    terminal_after_response = False
    try:
        created = await client.create_session(
            args.agent_id,
            user_id="kernel-codex-interaction-gate",
            expires_hours=1,
        )
        session_id = _session_id(created)
        receipt = await client.chat(
            args.agent_id,
            args.prompt,
            session_id=session_id,
        )

        deadline = time.monotonic() + args.wait_seconds
        while time.monotonic() < deadline:
            events = await asyncio.to_thread(
                _read_runtime_events,
                endpoint=endpoint,
                api_key=api_key,
                agent_id=args.agent_id,
                session_id=session_id,
            )
            requested = next(
                (
                    request
                    for event in events
                    if (request := _interaction_request(event)) is not None
                ),
                None,
            )
            if requested is not None:
                break
            # Message projection is informative only.  While a turn is paused
            # on approval the public projection can legitimately lag or time
            # out, so it must never decide whether the durable interaction
            # stream passed the gate.
            try:
                message_page = await client.list_session_messages(
                    agent_id=args.agent_id,
                    session_id=session_id,
                    limit=100,
                    include_reasoning=False,
                    include_tool_events=False,
                )
                messages = list(
                    message_page.get("messages") or message_page.get("Messages") or []
                )
            except Exception:
                messages = []
            # Legacy RunAgent mirrors can emit a terminal-looking run_status
            # before the later durable approval projection.  Only the
            # Interaction event decides this phase of the gate.
            await asyncio.sleep(args.poll_seconds)

        requested = next(
            (
                request
                for event in events
                if (request := _interaction_request(event)) is not None
            ),
            None,
        )
        if requested is not None and args.decision != "request-only":
            idempotency_key = (
                f"kernel-codex-interaction-{args.decision}-{uuid.uuid4().hex}"
            )
            response = {"decision": args.decision}
            submission = await client.submit_interaction(
                agent_id=args.agent_id,
                session_id=session_id,
                run_id=str(requested["run_id"]),
                interaction_id=str(requested["interaction_id"]),
                expected_revision=int(requested["revision"]),
                action=args.decision,
                response=response,
                idempotency_key=idempotency_key,
            )
            if args.verify_idempotency:
                duplicate = await client.submit_interaction(
                    agent_id=args.agent_id,
                    session_id=session_id,
                    run_id=str(requested["run_id"]),
                    interaction_id=str(requested["interaction_id"]),
                    expected_revision=int(requested["revision"]),
                    action=args.decision,
                    response=response,
                    idempotency_key=idempotency_key,
                )

            deadline = time.monotonic() + args.wait_seconds
            while time.monotonic() < deadline:
                events = await asyncio.to_thread(
                    _read_runtime_events,
                    endpoint=endpoint,
                    api_key=api_key,
                    agent_id=args.agent_id,
                    session_id=session_id,
                )
                resolved = next(
                    (
                        response_event
                        for event in events
                        if (
                            response_event := _interaction_response(event)
                        ) is not None
                        and response_event["interaction_id"]
                        == requested["interaction_id"]
                    ),
                    None,
                )
                if resolved is not None:
                    terminal_after_response = _terminal_after(
                        events, resolved.get("seq_id")
                    )
                    if terminal_after_response:
                        break
                await asyncio.sleep(args.poll_seconds)

        request_passed = requested is not None
        closure_passed = args.decision == "request-only" or (
            resolved is not None
            and terminal_after_response
            and (
                not args.verify_idempotency
                or (
                    _receipt_id(submission) is not None
                    and _receipt_id(submission) == _receipt_id(duplicate)
                )
            )
        )
        result = {
            "status": "pass" if request_passed and closure_passed else "fail",
            "agent_id": args.agent_id,
            "session_id": session_id,
            "duration_seconds": round(time.monotonic() - started, 3),
            "receipt_status": receipt.get("receipt_status") or receipt.get("ReceiptStatus"),
            "accepted_seq": receipt.get("accepted_seq") or receipt.get("AcceptedSeq"),
            "message_id": receipt.get("message_id") or receipt.get("MessageId"),
            "event_types": [_event_type(event) for event in events],
            "tool_events": [
                _tool_event_summary(event)
                for event in events
                if _event_type(event) == "tool_call"
            ],
            "assistant_message_ids": [
                message.get("message_id") or message.get("MessageId")
                for message in messages
                if str(message.get("role") or message.get("Role")) == "assistant"
            ],
            "assistant_previews": (
                [
                    str(
                        message.get("content")
                        or message.get("Content")
                        or message.get("text")
                        or message.get("Text")
                        or ""
                    )[: args.preview_chars]
                    for message in messages
                    if str(message.get("role") or message.get("Role")) == "assistant"
                ]
                if args.preview_chars > 0
                else []
            ),
            "terminal_event_ids": [
                _event_id(event)
                for event in events
                if _event_type(event) in {"run.completed", "run.failed", "run.cancelled"}
                or _event_type(event) == "run_status"
            ],
            "interaction": requested,
            "decision": args.decision,
            "interaction_response": resolved,
            "terminal_after_response": terminal_after_response,
            "submission_receipt_id": _receipt_id(submission),
            "duplicate_receipt_id": _receipt_id(duplicate),
            "idempotency_reused_receipt": (
                _receipt_id(submission) is not None
                and _receipt_id(submission) == _receipt_id(duplicate)
            )
            if args.verify_idempotency and args.decision != "request-only"
            else None,
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if request_passed and closure_passed else 1
    finally:
        if requested is not None and resolved is None:
            # A gate must never strand a live native approval.  Best effort is
            # intentionally before DeleteSession so the same process-local
            # provider handle can consume the rejection and terminate.
            try:
                await client.submit_interaction(
                    agent_id=args.agent_id,
                    session_id=str(session_id),
                    run_id=str(requested["run_id"]),
                    interaction_id=str(requested["interaction_id"]),
                    expected_revision=int(requested["revision"]),
                    action="reject",
                    response={"decision": "reject"},
                    idempotency_key=f"kernel-gate-cleanup-{uuid.uuid4().hex}",
                )
                await asyncio.sleep(min(args.poll_seconds, 1.0))
            except Exception:
                pass
        if session_id is not None:
            await client.delete_session(session_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--region", default="pre-online")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--wait-seconds", type=float, default=20.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--request-timeout", type=float, default=120.0)
    parser.add_argument(
        "--decision",
        choices=("approve", "reject", "request-only"),
        default="approve",
        help="close the real approval with this decision (default: approve)",
    )
    parser.add_argument(
        "--verify-idempotency",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="replay the exact SubmitInteraction idempotency key (default: enabled)",
    )
    parser.add_argument(
        "--preview-chars",
        type=int,
        default=0,
        help="include a bounded assistant preview for diagnosis (default: disabled)",
    )
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
