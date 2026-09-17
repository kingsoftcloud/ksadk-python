"""Deterministic local Chat Completions endpoint for real runtime vertical tests."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(frozen=True)
class CapturedChatRequest:
    path: str
    authorization: str
    payload: dict[str, Any]


class DeterministicChatCompletionsStub:
    def __init__(self) -> None:
        self._requests: list[CapturedChatRequest] = []
        self._lock = threading.Lock()
        self._server = _ChatServer(("127.0.0.1", 0), _ChatHandler, self)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="harness-chat-completions-stub",
            daemon=True,
        )

    def __enter__(self) -> "DeterministicChatCompletionsStub":
        self._thread.start()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    @property
    def endpoint_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1/chat/completions"

    def requests(self) -> list[CapturedChatRequest]:
        with self._lock:
            return list(self._requests)

    def _respond(self, path: str, authorization: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._requests.append(
                CapturedChatRequest(
                    path=path,
                    authorization=authorization,
                    payload=payload,
                )
            )
            index = len(self._requests)
        return {
            "id": f"chatcmpl-harness-{index}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": str(payload.get("model") or "fixture-model"),
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": f"scheduled harness result {index}",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }


class _ChatServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], owner):
        super().__init__(address, handler)
        self.owner = owner


class _ChatHandler(BaseHTTPRequestHandler):
    server: _ChatServer

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        length = int(self.headers.get("Content-Length") or "0")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_error(400)
            return
        response = self.server.owner._respond(
            self.path,
            self.headers.get("Authorization") or "",
            payload,
        )
        content_type = "application/json"
        if payload.get("stream"):
            # The production Harness streams model output. Preserve the same
            # deterministic answer and tool calls over the Chat Completions SSE
            # protocol instead of returning a non-streaming response to it.
            choice = response["choices"][0]
            delta = dict(choice["message"])
            if "tool_calls" in delta:
                delta["tool_calls"] = [
                    {"index": index, **call} for index, call in enumerate(delta["tool_calls"])
                ]
            chunk = {key: response[key] for key in ("id", "created", "model")}
            chunk["object"] = "chat.completion.chunk"
            events = [
                {**chunk, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {
                    **chunk,
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}
                    ],
                    "usage": response["usage"],
                },
            ]
            body = (
                "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
            ).encode("utf-8")
            content_type = "text/event-stream"
        else:
            body = json.dumps(response, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return
