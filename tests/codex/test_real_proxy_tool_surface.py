from __future__ import annotations

import asyncio
import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

openai_codex = pytest.importorskip("openai_codex")

from ksadk.codex.client import AsyncCodexClient  # noqa: E402
from ksadk.model_proxy.transform import Streamer  # noqa: E402


class _ChatUpstream(BaseHTTPRequestHandler):
    requests: list[dict] = []
    responses_requests: list[dict] = []
    rejected_tool_type = "namespace"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/responses"):
            type(self).responses_requests.append(body)
            items = body.get("input") if isinstance(body, dict) else None
            declared_types: set[str] = set()

            def collect_types(value: object) -> None:
                if isinstance(value, dict):
                    tool_type = value.get("type")
                    if isinstance(tool_type, str):
                        declared_types.add(tool_type)
                    for child in value.values():
                        collect_types(child)
                elif isinstance(value, list):
                    for child in value:
                        collect_types(child)

            collect_types(items or [])
            rejected = type(self).rejected_tool_type
            if rejected in declared_types:
                encoded = (
                    f"Invalid value: {rejected}, Supported values are: "
                    "function, mcp, knowledge_search"
                ).encode()
                self.send_response(400)
                self.send_header("Content-Type", "text/plain")
            elif body.get("stream") is True:
                streamer = Streamer("resp-native", str(body.get("model") or "model"))
                encoded = "".join(
                    [
                        *streamer.start(),
                        *streamer.handle(
                            {
                                "choices": [
                                    {
                                        "delta": {"content": "done"},
                                        "finish_reason": "stop",
                                    }
                                ]
                            }
                        ),
                        *streamer.finalize(),
                    ]
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
            else:
                encoded = json.dumps(
                    {"id": "resp-probe", "output": [], "status": "completed"}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        type(self).requests.append(body)
        chunks = [
            {
                "id": "chatcmpl-proxy-tool-surface",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "qwen3.7-flash",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "done"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-proxy-tool-surface",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "qwen3.7-flash",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        ]
        payload = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        encoded = payload.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


async def _collect_turn(client: AsyncCodexClient, thread_id: str) -> list[dict]:
    return [
        event
        async for event in client.run_turn(
            thread_id,
            "Use the command tool to print phase1-tool-surface.",
            config={},
        )
    ]


@pytest.mark.asyncio
async def test_real_codex_binary_sends_command_tools_through_chat_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Lock the real Codex -> Responses -> proxy -> Chat tool surface."""

    from ksadk.model_proxy import server as proxy_server

    raw_responses_requests: list[dict] = []
    original_transform = proxy_server.responses_to_chat

    def capture_transform(body: dict):
        raw_responses_requests.append(copy.deepcopy(body))
        return original_transform(body)

    monkeypatch.setattr(proxy_server, "responses_to_chat", capture_transform)
    _ChatUpstream.requests = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _ChatUpstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    host, port = upstream.server_address
    config = openai_codex.CodexConfig(
        cwd=str(tmp_path),
        env={
            "KSADK_CODEX_USE_PROXY": "1",
            "OPENAI_API_BASE": f"http://{host}:{port}/v1",
            "OPENAI_API_KEY": "test-upstream-key",
            "OPENAI_MODEL_NAME": "qwen3.7-flash",
        },
    )
    client = AsyncCodexClient(config=config)
    try:
        thread_id = await client.start_thread({})
        await asyncio.wait_for(_collect_turn(client, thread_id), timeout=20)
    finally:
        await client.close()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert raw_responses_requests
    first_request = raw_responses_requests[0]
    additional_tools = [
        item
        for item in first_request.get("input") or []
        if isinstance(item, dict) and item.get("type") == "additional_tools"
    ]
    assert additional_tools, "real Codex request did not declare dynamic tools"
    assert _ChatUpstream.requests
    request = next(
        (request for request in _ChatUpstream.requests if request.get("tools")),
        _ChatUpstream.requests[0],
    )
    tools = request.get("tools") or []
    names = {
        str((tool.get("function") or {}).get("name") or "")
        for tool in tools
        if isinstance(tool, dict)
    }
    assert tools, "real Codex request lost all tools in Responses -> Chat conversion"
    assert "functions__exec" in names


@pytest.mark.asyncio
async def test_auto_mode_proxies_when_responses_rejects_codex_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A text-only Responses 200 must not send real Codex namespace tools direct."""

    from ksadk.model_proxy import server as proxy_server

    raw_responses_requests: list[dict] = []
    original_transform = proxy_server.responses_to_chat

    def capture_transform(body: dict):
        raw_responses_requests.append(copy.deepcopy(body))
        return original_transform(body)

    monkeypatch.setattr(proxy_server, "responses_to_chat", capture_transform)
    _ChatUpstream.requests = []
    _ChatUpstream.responses_requests = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _ChatUpstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    host, port = upstream.server_address
    config = openai_codex.CodexConfig(
        cwd=str(tmp_path),
        env={
            "OPENAI_API_BASE": f"http://{host}:{port}/v1",
            "OPENAI_API_KEY": "test-auto-upstream-key",
            "OPENAI_MODEL_NAME": "responses-without-namespace",
        },
    )
    client = AsyncCodexClient(config=config)
    try:
        thread_id = await client.start_thread({})
        events = await asyncio.wait_for(_collect_turn(client, thread_id), timeout=20)
    finally:
        await client.close()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert len(_ChatUpstream.responses_requests) == 2
    assert raw_responses_requests, "real Codex request bypassed the selected proxy"
    assert _ChatUpstream.requests
    request = next(request for request in _ChatUpstream.requests if request.get("tools"))
    names = {
        str((tool.get("function") or {}).get("name") or "")
        for tool in request.get("tools") or []
        if isinstance(tool, dict)
    }
    assert "functions__exec" in names
    assert not any(
        event.get("method") == "error" and "Invalid value: namespace" in str(event.get("params"))
        for event in events
    )


@pytest.mark.asyncio
async def test_auto_mode_keeps_native_responses_and_disables_rejected_web_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A GLM-shaped web_search rejection stays native but omits web_search."""

    from ksadk.model_proxy import server as proxy_server

    class GlmWebSearchUpstream(_ChatUpstream):
        requests: list[dict] = []
        responses_requests: list[dict] = []
        rejected_tool_type = "web_search"

    raw_responses_requests: list[dict] = []
    original_transform = proxy_server.responses_to_chat

    def capture_transform(body: dict):
        raw_responses_requests.append(copy.deepcopy(body))
        return original_transform(body)

    monkeypatch.setattr(proxy_server, "responses_to_chat", capture_transform)
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), GlmWebSearchUpstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    host, port = upstream.server_address
    config = openai_codex.CodexConfig(
        cwd=str(tmp_path),
        env={
            "OPENAI_API_BASE": f"http://{host}:{port}/v1",
            "OPENAI_API_KEY": "test-glm-key",
            "OPENAI_MODEL_NAME": "glm-5.1",
        },
    )
    client = AsyncCodexClient(config=config)
    try:
        thread_id = await client.start_thread({})
        events = await asyncio.wait_for(_collect_turn(client, thread_id), timeout=20)
    finally:
        await client.close()
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    assert len(GlmWebSearchUpstream.responses_requests) == 5
    assert GlmWebSearchUpstream.responses_requests[3]["input"][0]["tools"] == [
        {"type": "web_search"}
    ]
    actual_request = GlmWebSearchUpstream.responses_requests[4]
    assert actual_request["stream"] is True
    assert "web_search" not in json.dumps(actual_request)
    assert not raw_responses_requests, "native Responses request unexpectedly used proxy"
    assert not GlmWebSearchUpstream.requests, "native Responses request reached Chat Completions"
    assert not any(
        event.get("method") == "error" and "Invalid value: web_search" in str(event.get("params"))
        for event in events
    )
