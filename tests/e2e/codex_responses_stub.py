"""Deterministic local Responses endpoint for real Codex App Server E2E tests."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(frozen=True)
class CapturedResponsesRequest:
    """One model request observed at the local stub boundary."""

    path: str
    payload: dict[str, Any]

    def input_texts(self, role: str) -> list[str]:
        texts: list[str] = []
        for item in self.payload.get("input", []):
            if not isinstance(item, dict) or item.get("role") != role:
                continue
            content = item.get("content")
            if isinstance(content, str):
                texts.append(content)
                continue
            if not isinstance(content, list):
                continue
            texts.extend(
                str(part["text"])
                for part in content
                if isinstance(part, dict)
                and part.get("type") in {"input_text", "output_text"}
                and isinstance(part.get("text"), str)
            )
        return texts


class DeterministicResponsesStub:
    """Return fixed text, or deterministically drive one native MCP round-trip."""

    def __init__(self, *, mcp_namespace: str | None = None) -> None:
        self._requests: list[CapturedResponsesRequest] = []
        self._lock = threading.Lock()
        self._mcp_namespace = mcp_namespace
        self._next_call = 1
        self._pending_values: dict[str, str] = {}
        self._server = _StubServer(("127.0.0.1", 0), _StubHandler, self)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="codex-plugin-responses-stub",
            daemon=True,
        )

    def __enter__(self) -> "DeterministicResponsesStub":
        self._thread.start()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"

    def single_request(self) -> CapturedResponsesRequest:
        requests = self.requests()
        if len(requests) != 1:
            raise AssertionError(f"expected one Responses request, got {len(requests)}")
        return requests[0]

    def requests(self) -> list[CapturedResponsesRequest]:
        """Return a stable snapshot of every request observed so far."""

        with self._lock:
            return list(self._requests)

    def _record(self, path: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._requests.append(CapturedResponsesRequest(path=path, payload=payload))

    def _events(self, payload: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        if self._mcp_namespace is None:
            return _message_events("bridge skill received", suffix="plugin-skill")

        with self._lock:
            pending_call_ids = frozenset(self._pending_values)
        tool_output = _function_call_output(payload, pending_call_ids)
        if tool_output is not None:
            call_id = str(tool_output.get("call_id") or "")
            with self._lock:
                self._pending_values.pop(call_id, None)
            value = _mcp_result_text(tool_output)
            return _message_events(
                f"MCP weather observation: {value}",
                suffix=f"mcp-final-{call_id}",
            )

        value = _latest_plain_user_text(payload)
        with self._lock:
            call_id = f"call-mcp-{self._next_call}"
            self._next_call += 1
            self._pending_values[call_id] = value
        item_id = f"fc-mcp-{call_id}"
        arguments = json.dumps({"value": value}, separators=(",", ":"))
        item = {
            "type": "function_call",
            "id": item_id,
            "call_id": call_id,
            "name": "lookup",
            "namespace": self._mcp_namespace,
            "arguments": arguments,
        }
        return (
            {
                "type": "response.created",
                "response": {"id": f"resp-{call_id}"},
            },
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**item, "arguments": ""},
            },
            {
                "type": "response.function_call_arguments.done",
                "item_id": item_id,
                "arguments": arguments,
            },
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": item,
            },
            _completed_event(f"resp-{call_id}", [item]),
        )


def _function_call_output(
    payload: dict[str, Any], pending_call_ids: frozenset[str]
) -> dict[str, Any] | None:
    for item in reversed(payload.get("input") or []):
        if (
            isinstance(item, dict)
            and item.get("type") == "function_call_output"
            and item.get("call_id") in pending_call_ids
        ):
            return item
    return None


def _latest_plain_user_text(payload: dict[str, Any]) -> str:
    for item in reversed(payload.get("input") or []):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in reversed(content):
            if not isinstance(part, dict) or part.get("type") != "input_text":
                continue
            text = str(part.get("text") or "")
            if not text.startswith("<skill>"):
                return text
    raise AssertionError("deterministic MCP response requires a plain user input")


def _mcp_result_text(tool_output: dict[str, Any]) -> str:
    output = str(tool_output.get("output") or "")
    marker = "Output:\n"
    encoded = output.rsplit(marker, 1)[-1].strip()
    try:
        result = json.loads(encoded)
    except json.JSONDecodeError as error:
        raise AssertionError(f"MCP output is not deterministic JSON: {output!r}") from error
    value = result.get("result") if isinstance(result, dict) else None
    if not isinstance(value, str) or not value:
        raise AssertionError(f"MCP output has no string result: {result!r}")
    return value


def _completed_event(response_id: str, output: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "output": output,
            "usage": {
                "input_tokens": 1,
                "input_tokens_details": None,
                "output_tokens": 1,
                "output_tokens_details": None,
                "total_tokens": 2,
            },
        },
    }


def _message_events(text: str, *, suffix: str) -> tuple[dict[str, Any], ...]:
    response_id = f"resp-{suffix}"
    item = {
        "type": "message",
        "role": "assistant",
        "id": f"msg-{suffix}",
        "content": [{"type": "output_text", "text": text}],
    }
    return (
        {"type": "response.created", "response": {"id": response_id}},
        {"type": "response.output_item.done", "item": item},
        _completed_event(response_id, [item]),
    )


class _StubServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        stub: DeterministicResponsesStub,
    ) -> None:
        super().__init__(address, handler)
        self.stub = stub


class _StubHandler(BaseHTTPRequestHandler):
    server: _StubServer

    def log_message(self, _format: str, *_args: object) -> None:
        return None

    def do_GET(self) -> None:
        if self.path.endswith("/models"):
            self._send_json(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "ksadk-codex-plugin-stub",
                            "object": "model",
                            "created": 0,
                            "owned_by": "test",
                        }
                    ],
                }
            )
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if not self.path.endswith("/responses"):
            self.send_error(404)
            return
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            self.send_error(400)
            return
        self.server.stub._record(self.path, payload)
        events = self.server.stub._events(payload)
        body = "".join(
            f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


__all__ = ["CapturedResponsesRequest", "DeterministicResponsesStub"]
