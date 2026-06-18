#!/usr/bin/env python3
"""Validate long-task resume through the public Hosted path.

This script targets the real Hosted route:

    PublicEndpoint -> agentengine-gateway -> agentengine-server -> runtime

It does not talk to the runtime pod directly and it does not need the PG DSN.
Use it after a long-task-capable LangGraph/ADK agent is already deployed.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx


DEFAULT_PROMPT = "run until checkpoint"
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "resume_failed"}


class HostedE2EError(AssertionError):
    pass


@dataclass
class HostedClient:
    base_url: str
    agent_id: str
    user_id: str
    timeout: float
    api_key: str = ""
    cookie: str = ""
    account_id: str = ""
    principal_id: str = ""

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/") + "/"
        headers = {
            "Accept": "application/json",
            "X-Ksc-Request-Id": f"hosted-e2e-{uuid.uuid4().hex}",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.cookie:
            headers["Cookie"] = self.cookie
        if self.account_id:
            headers["X-Auth-Account-Id"] = self.account_id
        if self.principal_id:
            headers["X-Ksc-User-uuid"] = self.principal_id
        self.client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(self.timeout, connect=min(self.timeout, 20.0)),
            follow_redirects=True,
            verify=False,
        )

    def close(self) -> None:
        self.client.close()

    def action(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(f"agentengine/api/v1/{name}", json=payload)
        if response.status_code >= 400:
            raise HostedE2EError(
                f"{name} HTTP {response.status_code}: {response.text[:1000]}"
            )
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise HostedE2EError(f"{name} returned non-JSON body: {response.text[:1000]}") from exc
        if isinstance(body, dict) and body.get("Code") not in (None, 0):
            raise HostedE2EError(f"{name} returned Code={body.get('Code')}: {body}")
        return body

    def stream_action(self, name: str, payload: dict[str, Any], *, max_seconds: float) -> str:
        chunks: list[str] = []
        deadline = time.monotonic() + max_seconds
        with self.client.stream(
            "POST",
            f"agentengine/api/v1/{name}",
            json=payload,
            headers={"Accept": "text/event-stream"},
        ) as response:
            if response.status_code >= 400:
                text = response.read().decode("utf-8", "replace")
                raise HostedE2EError(f"{name} stream HTTP {response.status_code}: {text[:1000]}")
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" not in content_type.lower():
                raise HostedE2EError(f"{name} did not return SSE content-type: {content_type}")
            for line in response.iter_lines():
                if time.monotonic() > deadline:
                    raise HostedE2EError(f"{name} stream timed out after {max_seconds}s")
                if line:
                    chunks.append(line)
        return "\n".join(chunks)


def _data(payload: dict[str, Any], action: str) -> dict[str, Any]:
    data = payload.get("Data")
    if not isinstance(data, dict):
        raise HostedE2EError(f"{action} missing Data object: {payload}")
    return data


def _extract_capabilities(bootstrap: dict[str, Any]) -> dict[str, Any]:
    capabilities = _data(bootstrap, "GetAgentUiBootstrap").get("Capabilities")
    if not isinstance(capabilities, dict):
        raise HostedE2EError(f"GetAgentUiBootstrap missing Capabilities: {bootstrap}")
    return capabilities


def _assert_checkpoint_capability(capabilities: dict[str, Any]) -> None:
    run_lifecycle = capabilities.get("RunLifecycle")
    if not isinstance(run_lifecycle, dict):
        raise HostedE2EError(f"Capabilities.RunLifecycle is missing: {capabilities}")
    missing = [
        key
        for key in ("Checkpoints", "CheckpointResume")
        if run_lifecycle.get(key) is not True
    ]
    if missing:
        raise HostedE2EError(
            "Hosted bootstrap does not advertise checkpoint lifecycle: "
            f"missing_true={missing}, RunLifecycle={run_lifecycle}"
        )


def _make_responses_input(prompt: str) -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": prompt,
                }
            ],
        }
    ]


def _run_agent(client: HostedClient, *, session_id: str, prompt: str) -> dict[str, Any]:
    return client.action(
        "RunAgent",
        {
            "AgentId": client.agent_id,
            "UserId": client.user_id,
            "SessionId": session_id,
            "ApiFormat": "responses",
            "Stream": False,
            "ResponsesInput": _make_responses_input(prompt),
        },
    )


def _stream_run_agent_background(
    client: HostedClient,
    *,
    session_id: str,
    prompt: str,
    invocation_id: str,
    max_seconds: float,
) -> tuple[threading.Thread, dict[str, Any]]:
    result: dict[str, Any] = {"sse": "", "error": None}

    def _run() -> None:
        try:
            result["sse"] = client.stream_action(
                "RunAgent",
                {
                    "AgentId": client.agent_id,
                    "UserId": client.user_id,
                    "SessionId": session_id,
                    "InvocationId": invocation_id,
                    "ApiFormat": "responses",
                    "Stream": True,
                    "ResponsesInput": _make_responses_input(prompt),
                },
                max_seconds=max_seconds,
            )
        except Exception as exc:  # pragma: no cover - surfaced by caller in integration mode.
            result["error"] = exc

    thread = threading.Thread(target=_run, name=f"hosted-e2e-stream-{invocation_id}", daemon=True)
    thread.start()
    return thread, result


def _list_checkpoints(client: HostedClient, *, session_id: str, run_id: str = "") -> list[dict[str, Any]]:
    payload: dict[str, Any] = {
        "AgentId": client.agent_id,
        "SessionId": session_id,
    }
    if run_id:
        payload["RunId"] = run_id
    response = client.action("ListSessionCheckpoints", payload)
    checkpoints = _data(response, "ListSessionCheckpoints").get("Checkpoints")
    if not isinstance(checkpoints, list):
        raise HostedE2EError(f"ListSessionCheckpoints missing Checkpoints list: {response}")
    return checkpoints


def _is_retryable_checkpoint_not_found(exc: HostedE2EError) -> bool:
    text = str(exc)
    return "ListSessionCheckpoints" in text and (
        "Code=404" in text
        or "HTTP 404" in text
        or "not found" in text.lower()
        or "资源不存在" in text
    )


def _list_events(client: HostedClient, *, session_id: str) -> list[dict[str, Any]]:
    response = client.action(
        "ListSessionEvents",
        {"AgentId": client.agent_id, "SessionId": session_id},
    )
    events = _data(response, "ListSessionEvents").get("Events")
    if not isinstance(events, list):
        raise HostedE2EError(f"ListSessionEvents missing Events list: {response}")
    return events


def _event_type_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        event_type = str(event.get("EventType") or "")
        counts[event_type] = counts.get(event_type, 0) + 1
    return counts


def _wait_for_checkpoint(
    client: HostedClient,
    *,
    session_id: str,
    run_id: str = "",
    attempts: int,
    interval: float,
) -> dict[str, Any]:
    last_checkpoints: list[dict[str, Any]] = []
    for _ in range(attempts):
        try:
            last_checkpoints = _list_checkpoints(client, session_id=session_id, run_id=run_id)
        except HostedE2EError as exc:
            if not _is_retryable_checkpoint_not_found(exc):
                raise
            last_checkpoints = []
        if last_checkpoints:
            return last_checkpoints[0]
        time.sleep(interval)
    events = _list_events(client, session_id=session_id)
    raise HostedE2EError(
        "No checkpoint appeared through Hosted facade; "
        f"last_checkpoints={last_checkpoints}, event_counts={_event_type_counts(events)}"
    )


def _maybe_preview(client: HostedClient, *, session_id: str, run_id: str, checkpoint_id: str) -> dict[str, Any]:
    try:
        return client.action(
            "PreviewCheckpointResume",
            {
                "AgentId": client.agent_id,
                "SessionId": session_id,
                "RunId": run_id,
                "CheckpointId": checkpoint_id,
            },
        )
    except HostedE2EError as exc:
        return {"skipped": True, "error": str(exc)}


def _maybe_list_tool_receipts(
    client: HostedClient,
    *,
    session_id: str,
    run_id: str,
    checkpoint_id: str,
) -> dict[str, Any]:
    try:
        return client.action(
            "ListToolReceipts",
            {
                "AgentId": client.agent_id,
                "SessionId": session_id,
                "RunId": run_id,
                "CheckpointId": checkpoint_id,
            },
        )
    except HostedE2EError as exc:
        return {"skipped": True, "error": str(exc)}


def _resume_stream(
    client: HostedClient,
    *,
    session_id: str,
    run_id: str,
    checkpoint_id: str,
    invocation_id: str,
    max_seconds: float,
) -> str:
    return client.stream_action(
        "ResumeRun",
        {
            "AgentId": client.agent_id,
            "SessionId": session_id,
            "RunId": run_id,
            "CheckpointId": checkpoint_id,
            "InvocationId": invocation_id,
            "Stream": True,
        },
        max_seconds=max_seconds,
    )


def _cancel_run(client: HostedClient, *, invocation_id: str) -> dict[str, Any]:
    return client.action(
        "CancelRun",
        {
            "AgentId": client.agent_id,
            "InvocationId": invocation_id,
        },
    )


def _event_statuses(events: list[dict[str, Any]], *, invocation_id: str = "") -> list[str]:
    statuses: list[str] = []
    for event in events:
        if invocation_id and str(event.get("InvocationId") or "") != invocation_id:
            continue
        if event.get("EventType") != "run_status":
            continue
        content = event.get("Content")
        if isinstance(content, dict) and content.get("status"):
            statuses.append(str(content["status"]))
    return statuses


def validate_checkpoint_resume(
    client: HostedClient,
    *,
    session_id: str,
    prompt: str,
    wait_attempts: int,
    wait_interval: float,
    stream_timeout: float,
) -> dict[str, Any]:
    bootstrap = client.action(
        "GetAgentUiBootstrap",
        {"AgentId": client.agent_id, "SessionId": session_id},
    )
    capabilities = _extract_capabilities(bootstrap)
    _assert_checkpoint_capability(capabilities)

    run_payload = _run_agent(client, session_id=session_id, prompt=prompt)
    checkpoint = _wait_for_checkpoint(
        client,
        session_id=session_id,
        attempts=wait_attempts,
        interval=wait_interval,
    )
    run_id = str(checkpoint.get("RunId") or "").strip()
    checkpoint_id = str(checkpoint.get("CheckpointId") or "").strip()
    if not run_id or not checkpoint_id:
        raise HostedE2EError(f"Checkpoint missing RunId/CheckpointId: {checkpoint}")

    preview = _maybe_preview(
        client,
        session_id=session_id,
        run_id=run_id,
        checkpoint_id=checkpoint_id,
    )
    tool_receipts = _maybe_list_tool_receipts(
        client,
        session_id=session_id,
        run_id=run_id,
        checkpoint_id=checkpoint_id,
    )

    resume_invocation_id = f"run_{uuid.uuid4().hex}"
    resume_sse = _resume_stream(
        client,
        session_id=session_id,
        run_id=run_id,
        checkpoint_id=checkpoint_id,
        invocation_id=resume_invocation_id,
        max_seconds=stream_timeout,
    )
    events = _list_events(client, session_id=session_id)
    event_counts = _event_type_counts(events)
    statuses = _event_statuses(events, invocation_id=resume_invocation_id)

    if event_counts.get("run_resume", 0) < 1:
        raise HostedE2EError(f"ResumeRun did not create run_resume event: {event_counts}")
    if event_counts.get("run_checkpoint", 0) < 1:
        raise HostedE2EError(f"Session has no run_checkpoint events after resume: {event_counts}")
    if statuses and statuses[-1] not in TERMINAL_STATUSES:
        raise HostedE2EError(f"ResumeRun terminal status not reached: {statuses}")

    return {
        "status": "pass",
        "session_id": session_id,
        "run_id": run_id,
        "checkpoint_id": checkpoint_id,
        "resume_invocation_id": resume_invocation_id,
        "bootstrap_run_lifecycle": capabilities.get("RunLifecycle"),
        "run_agent_code": run_payload.get("Code"),
        "checkpoint_count": len(_list_checkpoints(client, session_id=session_id)),
        "event_counts": event_counts,
        "resume_statuses": statuses,
        "resume_sse_line_count": len([line for line in resume_sse.splitlines() if line.strip()]),
        "preview": _summarize_optional_action(preview, "Preview"),
        "tool_receipts": _summarize_optional_action(tool_receipts, "ToolReceipts"),
    }


def _summarize_optional_action(payload: dict[str, Any], data_key: str) -> dict[str, Any]:
    if payload.get("skipped"):
        return {"status": "skipped", "error": payload.get("error")}
    data = payload.get("Data") if isinstance(payload.get("Data"), dict) else {}
    value = data.get(data_key)
    if isinstance(value, list):
        return {"status": "pass", "count": len(value)}
    if isinstance(value, dict):
        return {"status": "pass", "keys": sorted(value.keys())}
    return {"status": "pass", "present": value is not None}


def validate_cancel_active(
    client: HostedClient,
    *,
    session_id: str,
    invocation_id: str,
    wait_attempts: int,
    wait_interval: float,
) -> dict[str, Any]:
    cancel_payload = _cancel_run(client, invocation_id=invocation_id)
    cancel_data = _data(cancel_payload, "CancelRun")
    for _ in range(wait_attempts):
        events = _list_events(client, session_id=session_id)
        statuses = _event_statuses(events, invocation_id=invocation_id)
        if statuses and statuses[-1] in TERMINAL_STATUSES:
            return {
                "status": "pass",
                "session_id": session_id,
                "invocation_id": invocation_id,
                "cancel_data": cancel_data,
                "statuses": statuses,
                "event_counts": _event_type_counts(events),
            }
        time.sleep(wait_interval)
    raise HostedE2EError(
        f"CancelRun did not reach terminal status for {invocation_id}: {cancel_data}"
    )


def validate_cancel_then_resume(
    client: HostedClient,
    *,
    session_id: str,
    prompt: str,
    wait_attempts: int,
    wait_interval: float,
    stream_timeout: float,
) -> dict[str, Any]:
    bootstrap = client.action(
        "GetAgentUiBootstrap",
        {"AgentId": client.agent_id, "SessionId": session_id},
    )
    capabilities = _extract_capabilities(bootstrap)
    _assert_checkpoint_capability(capabilities)

    invocation_id = f"run_{uuid.uuid4().hex}"
    stream_thread, stream_result = _stream_run_agent_background(
        client,
        session_id=session_id,
        prompt=prompt,
        invocation_id=invocation_id,
        max_seconds=stream_timeout,
    )

    try:
        checkpoint = _wait_for_checkpoint(
            client,
            session_id=session_id,
            run_id=invocation_id,
            attempts=wait_attempts,
            interval=wait_interval,
        )
        run_id = str(checkpoint.get("RunId") or "").strip()
        checkpoint_id = str(checkpoint.get("CheckpointId") or "").strip()
        if run_id != invocation_id:
            raise HostedE2EError(
                f"Checkpoint RunId should match active invocation_id: {run_id!r} != {invocation_id!r}"
            )
        if not checkpoint_id:
            raise HostedE2EError(f"Checkpoint missing CheckpointId: {checkpoint}")

        cancel_payload = _cancel_run(client, invocation_id=invocation_id)
        cancel_data = _data(cancel_payload, "CancelRun")
        if cancel_data.get("Cancelled") is not True:
            raise HostedE2EError(f"CancelRun did not accept active stream: {cancel_data}")

        cancelled_statuses: list[str] = []
        event_count_at_cancel = 0
        for _ in range(wait_attempts):
            events = _list_events(client, session_id=session_id)
            cancelled_statuses = _event_statuses(events, invocation_id=invocation_id)
            if cancelled_statuses and cancelled_statuses[-1] == "cancelled":
                event_count_at_cancel = len(events)
                break
            time.sleep(wait_interval)
        else:
            raise HostedE2EError(
                f"CancelRun did not create cancelled status for {invocation_id}: {cancel_data}"
            )

        stream_thread.join(timeout=min(stream_timeout, 10.0))
        if stream_thread.is_alive():
            raise HostedE2EError("RunAgent stream did not close after CancelRun")
        if stream_result.get("error") is not None:
            raise HostedE2EError(f"RunAgent stream failed during cancel validation: {stream_result['error']}")

        post_cancel_events = _list_events(client, session_id=session_id)
        unexpected_post_cancel = [
            event
            for event in post_cancel_events[event_count_at_cancel:]
            if event.get("EventType") in {"assistant_message", "run_checkpoint"}
            or (
                event.get("EventType") == "run_status"
                and isinstance(event.get("Content"), dict)
                and event["Content"].get("status") == "completed"
            )
        ]
        if unexpected_post_cancel:
            raise HostedE2EError(
                "Unexpected assistant/checkpoint/completed events appeared after cancel: "
                f"{_event_type_counts(unexpected_post_cancel)}"
            )

        resume_invocation_id = f"run_{uuid.uuid4().hex}"
        resume_sse = _resume_stream(
            client,
            session_id=session_id,
            run_id=run_id,
            checkpoint_id=checkpoint_id,
            invocation_id=resume_invocation_id,
            max_seconds=stream_timeout,
        )

        final_events = _list_events(client, session_id=session_id)
        final_counts = _event_type_counts(final_events)
        resume_statuses = _event_statuses(final_events, invocation_id=resume_invocation_id)
        if final_counts.get("run_resume", 0) < 1:
            raise HostedE2EError(f"ResumeRun after cancel did not create run_resume event: {final_counts}")
        if final_counts.get("run_checkpoint", 0) < 2:
            raise HostedE2EError(
                f"ResumeRun after cancel should leave at least two checkpoint events: {final_counts}"
            )
        if resume_statuses and resume_statuses[-1] not in TERMINAL_STATUSES:
            raise HostedE2EError(f"ResumeRun after cancel did not reach terminal status: {resume_statuses}")

        return {
            "status": "pass",
            "session_id": session_id,
            "run_id": run_id,
            "checkpoint_id": checkpoint_id,
            "cancel_invocation_id": invocation_id,
            "resume_invocation_id": resume_invocation_id,
            "bootstrap_run_lifecycle": capabilities.get("RunLifecycle"),
            "cancel_data": cancel_data,
            "cancel_statuses": cancelled_statuses,
            "event_counts": final_counts,
            "resume_statuses": resume_statuses,
            "stream_closed_after_cancel": not stream_thread.is_alive(),
            "run_sse_line_count": len(
                [line for line in str(stream_result.get("sse") or "").splitlines() if line.strip()]
            ),
            "resume_sse_line_count": len([line for line in resume_sse.splitlines() if line.strip()]),
        }
    finally:
        if stream_thread.is_alive():
            stream_thread.join(timeout=1.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--endpoint",
        required=True,
        help="PublicEndpoint, e.g. https://ar-xxx.agent-pre.kspmas.ksyun.com",
    )
    parser.add_argument("--agent-id", required=True, help="Agent runtime ID, e.g. ar-...")
    parser.add_argument("--api-key", default="", help="Optional runtime API key for public endpoint auth.")
    parser.add_argument(
        "--cookie",
        default="",
        help="Optional Cookie header value, e.g. ae_ui_session=... from a private/share link.",
    )
    parser.add_argument("--account-id", default="", help="Optional account id header.")
    parser.add_argument("--principal-id", default="", help="Optional principal/user id header.")
    parser.add_argument("--user-id", default="hosted-long-task-e2e-user")
    parser.add_argument("--session-id", default="", help="Defaults to a generated sess_... id.")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--stream-timeout", type=float, default=120.0)
    parser.add_argument("--wait-attempts", type=int, default=60)
    parser.add_argument("--wait-interval", type=float, default=1.0)
    parser.add_argument(
        "--mode",
        choices=["checkpoint-resume", "cancel-active", "cancel-then-resume"],
        default="checkpoint-resume",
    )
    parser.add_argument(
        "--invocation-id",
        default="",
        help="Required for --mode cancel-active; must be an active run invocation id.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    session_id = args.session_id or f"sess_{uuid.uuid4().hex}"
    client = HostedClient(
        base_url=args.endpoint,
        agent_id=args.agent_id,
        user_id=args.user_id,
        timeout=args.timeout,
        api_key=args.api_key,
        cookie=args.cookie,
        account_id=args.account_id,
        principal_id=args.principal_id,
    )
    try:
        if args.mode == "checkpoint-resume":
            result = validate_checkpoint_resume(
                client,
                session_id=session_id,
                prompt=args.prompt,
                wait_attempts=args.wait_attempts,
                wait_interval=args.wait_interval,
                stream_timeout=args.stream_timeout,
            )
        else:
            if args.mode == "cancel-then-resume":
                result = validate_cancel_then_resume(
                    client,
                    session_id=session_id,
                    prompt=args.prompt,
                    wait_attempts=args.wait_attempts,
                    wait_interval=args.wait_interval,
                    stream_timeout=args.stream_timeout,
                )
            elif not args.invocation_id:
                raise HostedE2EError("--invocation-id is required for --mode cancel-active")
            else:
                result = validate_cancel_active(
                    client,
                    session_id=session_id,
                    invocation_id=args.invocation_id,
                    wait_attempts=args.wait_attempts,
                    wait_interval=args.wait_interval,
                )
        print(json.dumps({"hosted_long_task_e2e": result}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "hosted_long_task_e2e": {
                        "status": "fail",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
