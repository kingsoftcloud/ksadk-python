"""Read the native stream bound by Server to one admitted enqueue command."""

from __future__ import annotations

import json
import time

import click
import requests


def _lines(response):
    # Requests.iter_lines can emit a spurious blank when CRLF spans chunks;
    # an SSE blank commits a frame, so split only complete LF-terminated lines.
    pending = ""
    for chunk in response.iter_content(chunk_size=1, decode_unicode=True):
        pending += chunk
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            yield line.removesuffix("\r")


class HostedCommandEvents:
    """Reconnect observations only; never enqueue or infer the latest run."""

    def __init__(self, transport, *, endpoint, agent_id, session_id, command_id, deadline):
        self.transport = transport
        self.url = endpoint.rstrip("/") + "/agentengine/api/v1/SubscribeSessionEvents"
        self.scope = {"AgentId": agent_id, "SessionId": session_id, "CommandId": command_id}
        self.deadline = deadline
        self.response = self.lines = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.response is not None:
            self.response.close()
        self.response = self.lines = None

    def _open(self, cursor, remaining):
        self.response = self.transport.get(
            self.url,
            params={**self.scope, "after_seq": cursor},
            headers={"Accept": "text/event-stream"},
            stream=True,
            timeout=min(45, remaining),
            allow_redirects=False,
        )
        response = self.response
        if response.is_redirect:
            raise click.ClickException("Hosted subscription redirected; request was not replayed")
        if response.headers.get("Content-Type", "").split(";", 1)[0].lower() != "text/event-stream":
            try:
                envelope = response.json()
            except ValueError:
                envelope = {}
            details = envelope.get("Data") if isinstance(envelope, dict) else None
            if (
                response.status_code in (200, 409)
                and isinstance(envelope, dict)
                and envelope.get("Code") == 409
                and isinstance(details, dict)
                and details.get("ReasonCode") == "KERNEL_COMMAND_PENDING"
            ):
                self.close()
                return False
            response.raise_for_status()
            raise click.ClickException(
                "Hosted command subscription was rejected; inspect the session above"
            )
        response.raise_for_status()
        response.encoding = "utf-8"
        self.lines = _lines(response)
        return True

    def read(self, cursor, remaining):
        """Translate one native frame into the existing hosted reader shape."""
        empty = {
            "EventOrder": "session_seq",
            "CursorSource": "runtime",
            "HasMore": False,
            "Events": [],
            "NextCursor": cursor,
        }
        try:
            if self.lines is None and not self._open(cursor, remaining):
                return empty
            data = []
            for line in self.lines:
                if time.monotonic() >= self.deadline:
                    self.close()
                    return empty
                if line.startswith("data:"):
                    data.append(line[5:].lstrip(" "))
                if line or not data:
                    continue
                try:
                    frame = json.loads("\n".join(data))
                except ValueError as exc:
                    raise click.ClickException(
                        "Hosted command stream returned invalid JSON"
                    ) from exc
                data = []
                if (
                    not isinstance(frame, dict)
                    or type(frame.get("seq")) is not int
                    or frame["seq"] <= 0
                    or frame.get("session_id") not in (None, self.scope["SessionId"])
                    or not isinstance(frame.get("run_id"), str)
                    or not frame["run_id"]
                ):
                    raise click.ClickException(
                        "Hosted command stream returned an invalid event identity"
                    )
                if frame["seq"] <= cursor:
                    continue  # Replayed observations never authorize another submission.
                family = frame.get("family")
                if (
                    family not in ("runtime", "interaction", "control")
                    or frame.get("family_version") != (2 if family == "runtime" else 1)
                    or (family == "runtime" and frame.get("schema_version") != 2)
                ):
                    raise click.ClickException(
                        "Hosted command stream returned an unsupported event family"
                    )
                content = (
                    {"runtime_event": frame}
                    if family == "runtime"
                    else {"session_event": {"payload": frame}}
                )
                row = {
                    "EventId": frame.get("event_id") or str(frame["seq"]),
                    "SeqId": frame["seq"],
                    "EventType": frame.get("event_type"),
                    "InvocationId": frame["run_id"],
                    "Content": content,
                    "Metadata": {"ksadk_canonical_runtime_event": family == "runtime"},
                }
                return {**empty, "Events": [row], "NextCursor": frame["seq"]}
            self.close()
            return empty
        except (requests.ConnectionError, requests.Timeout):
            self.close()
            raise
