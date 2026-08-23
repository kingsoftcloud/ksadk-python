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
from typing import Any

import requests

from ksadk.api.client import AgentEngineClient

DEFAULT_PROMPT = (
    "请使用 run_command 工具执行命令 echo phase1-interaction-gate；"
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
    value = event.get("SeqId") or event.get("seq_id")
    return int(value) if value is not None else None


def _event_id(event: dict[str, Any]) -> str | None:
    value = event.get("EventId") or event.get("event_id")
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
    response = requests.post(
        endpoint.rstrip("/") + "/agentengine/api/v1/ListSessionEvents",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"AgentId": agent_id, "SessionId": session_id, "Limit": 300},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json().get("Data") or {}
    return list(payload.get("Events") or payload.get("events") or [])


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
    try:
        created = await client.create_session(
            args.agent_id,
            user_id="phase1-codex-interaction-gate",
            expires_hours=1,
        )
        session_id = _session_id(created)
        receipt = await client.chat(
            args.agent_id,
            args.prompt,
            session_id=session_id,
        )

        deadline = time.monotonic() + args.wait_seconds
        requested: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            events = await asyncio.to_thread(
                _read_runtime_events,
                endpoint=endpoint,
                api_key=api_key,
                agent_id=args.agent_id,
                session_id=session_id,
            )
            requested = next(
                (event for event in events if _event_type(event) == "interaction.requested"),
                None,
            )
            if requested is not None:
                break
            message_page = await client.list_session_messages(
                agent_id=args.agent_id,
                session_id=session_id,
                limit=100,
                include_reasoning=False,
                include_tool_events=False,
            )
            messages = list(message_page.get("messages") or message_page.get("Messages") or [])
            if any(
                str(message.get("role") or message.get("Role")) == "assistant"
                for message in messages
            ):
                break
            terminal = any(
                _event_type(event) in {"run.completed", "run.failed", "run.cancelled"}
                or (
                    _event_type(event) == "run_status"
                    and str((event.get("Content") or event.get("content") or {}).get("status"))
                    in {"completed", "failed", "cancelled"}
                )
                for event in events
            )
            if terminal:
                break
            await asyncio.sleep(args.poll_seconds)

        requested = next(
            (event for event in events if _event_type(event) == "interaction.requested"),
            None,
        )
        result = {
            "status": "pass" if requested is not None else "fail",
            "agent_id": args.agent_id,
            "session_id": session_id,
            "duration_seconds": round(time.monotonic() - started, 3),
            "receipt_status": receipt.get("receipt_status") or receipt.get("ReceiptStatus"),
            "accepted_seq": receipt.get("accepted_seq") or receipt.get("AcceptedSeq"),
            "message_id": receipt.get("message_id") or receipt.get("MessageId"),
            "event_types": [_event_type(event) for event in events],
            "assistant_message_ids": [
                message.get("message_id") or message.get("MessageId")
                for message in messages
                if str(message.get("role") or message.get("Role")) == "assistant"
            ],
            "terminal_event_ids": [
                _event_id(event)
                for event in events
                if _event_type(event) in {"run.completed", "run.failed", "run.cancelled"}
                or _event_type(event) == "run_status"
            ],
            "interaction": (
                {
                    "event_id": _event_id(requested),
                    "seq_id": _event_seq(requested),
                }
                if requested is not None
                else None
            ),
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if requested is not None else 1
    finally:
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
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
