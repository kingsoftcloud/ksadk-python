"""server 层测试:create_app 鉴权 / 不支持工具 400 / ProxyServer 生命周期(含泄漏回归)。

均不打真实上游(鉴权与 400 在接触上游前返回;ProxyServer 用 /healthz,不触上游)。
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from fastapi.testclient import TestClient

from ksadk.model_proxy import ProxyConfig, ProxyServer
from ksadk.model_proxy.server import create_app

UPSTREAM = "https://upstream.example/v1"  # 假上游,不会被真正打到


def _cfg(**kw):
    d = {"upstream_base": UPSTREAM, "api_key": "sk-upstream", "local_token": "tok123"}
    d.update(kw)
    return ProxyConfig(**d)


# ---- create_app 鉴权 / 错误 ----


def test_auth_missing_token_401():
    r = TestClient(create_app(_cfg())).post("/v1/responses", json={"model": "m", "input": "hi"})
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "authentication_error"


def test_auth_wrong_token_401():
    r = TestClient(create_app(_cfg())).post(
        "/v1/responses", json={"model": "m", "input": "hi"}, headers={"Authorization": "Bearer x"}
    )
    assert r.status_code == 401


def test_unsupported_tools_fail_fast_400():
    # web_search 非 function/namespace,无法拍平也无法转 chat -> fail-fast 400
    body = {"model": "m", "input": "hi", "tools": [{"type": "web_search"}]}
    r = TestClient(create_app(_cfg())).post(
        "/v1/responses", json=body, headers={"Authorization": "Bearer tok123"}
    )
    assert r.status_code == 400
    assert r.json()["error"] == {
        "type": "unsupported_tools",
        "message": "The request uses tools unsupported by the model upstream.",
    }


def test_config_rejects_plaintext_http_upstream():
    with pytest.raises(ValueError, match="https"):
        ProxyConfig(upstream_base="http://kspmas.ksyun.com/v1", api_key="sk")


def test_config_allows_loopback_http():
    cfg = ProxyConfig(upstream_base="http://127.0.0.1:9999/v1", api_key="sk")
    assert cfg.upstream_base == "http://127.0.0.1:9999/v1"


# ---- ProxyServer 生命周期(含线程泄漏回归) ----


def _healthz(srv: ProxyServer) -> int:
    return httpx.get(f"http://{srv.host}:{srv.port}/healthz", timeout=2).status_code


def test_proxyserver_start_idempotent_and_stop_reclaims():
    srv = ProxyServer(_cfg())
    url1 = srv.start()
    assert _healthz(srv) == 200
    # 幂等:重复 start 返回同一地址,不新建线程/server
    url2 = srv.start()
    assert url1 == url2
    # stop 干净回收(泄漏回归:旧版 start 两次后 stop 只停最后一个,healthz 仍 200)
    srv.stop()
    with pytest.raises(Exception):  # noqa: B017,PT011 — 连接拒绝即视为已回收
        _healthz(srv)


def test_proxyserver_stop_idempotent():
    srv = ProxyServer(_cfg())
    srv.start()
    srv.stop()
    srv.stop()  # 重复 stop 不报错


def test_proxyserver_stop_does_not_report_lifespan_cancelled_error(capfd):
    """A completed turn must not make normal proxy cleanup look like a crash."""

    srv = ProxyServer(_cfg())
    srv.start()
    srv.stop()

    captured = capfd.readouterr()
    assert "Traceback (most recent call last)" not in captured.err
    assert "asyncio.exceptions.CancelledError" not in captured.err


# ---- 凭证/监听安全 ----


def test_config_rejects_uppercase_http_scheme():
    with pytest.raises(ValueError, match="https"):
        ProxyConfig(upstream_base="HTTP://example.com/v1", api_key="sk")


def test_config_rejects_non_http_scheme():
    with pytest.raises(ValueError, match="scheme"):
        ProxyConfig(upstream_base="ftp://example.com/v1", api_key="sk")


def test_non_loopback_host_requires_token():
    with pytest.raises(ValueError, match="local_token"):
        ProxyServer(_cfg(local_token=""), host="0.0.0.0")


class _RequestIdUpstream(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or "0")
        self.rfile.read(length)
        payload = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": "glm-5.2",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-Id", "xingliu-request-123")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def test_proxy_observer_reports_response_translation_and_upstream_request_id():
    upstream = HTTPServer(("127.0.0.1", 0), _RequestIdUpstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    observed: list[tuple[str, dict]] = []
    config = ProxyConfig(
        upstream_base=f"http://127.0.0.1:{upstream.server_address[1]}/v1",
        api_key="secret-upstream-key",
        local_token="local-token",
        event_callback=lambda event, data: observed.append((event, data)),
    )
    try:
        response = TestClient(create_app(config)).post(
            "/v1/responses",
            json={"model": "glm-5.2", "input": "hello"},
            headers={"Authorization": "Bearer local-token"},
        )
    finally:
        upstream.shutdown()
        upstream.server_close()

    assert response.status_code == 200
    assert [event for event, _ in observed] == [
        "proxy.requested",
        "proxy.upstream",
        "proxy.completed",
    ]
    assert observed[0][1]["model"] == "glm-5.2"
    assert observed[0][1]["protocol"] == "responses-to-chat"
    assert observed[1][1] == {
        "requestId": "xingliu-request-123",
        "statusCode": 200,
    }
    assert observed[2][1]["responseId"].startswith("resp_")
    assert observed[2][1]["model"] == "glm-5.2"
    assert observed[2][1]["statusCode"] == 200
    assert observed[2][1]["durationMs"] >= 0
    assert observed[2][1]["usage"] == {
        "inputTokens": 1,
        "outputTokens": 1,
        "totalTokens": 2,
        "cachedInputTokens": 0,
        "reasoningOutputTokens": 0,
    }
    assert "secret-upstream-key" not in json.dumps(observed)


class _RecordingUpstream(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or "0")
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).received.append(body)
        payload = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": body.get("model"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass


def test_responses_rewrites_pseudo_model_to_configured_upstream_model():
    # codex auto_review guardian 发内部伪模型名 codex-auto-review;
    # 单上游代理必须改写为配置的真实模型,否则上游按未知模型 403。
    _RecordingUpstream.received = []
    upstream = HTTPServer(("127.0.0.1", 0), _RecordingUpstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    observed: list[tuple[str, dict]] = []
    config = ProxyConfig(
        upstream_base=f"http://127.0.0.1:{upstream.server_address[1]}/v1",
        api_key="secret-upstream-key",
        local_token="local-token",
        upstream_model="glm-5.2",
        event_callback=lambda event, data: observed.append((event, data)),
    )
    try:
        response = TestClient(create_app(config)).post(
            "/v1/responses",
            json={"model": "codex-auto-review", "input": "review this"},
            headers={"Authorization": "Bearer local-token"},
        )
    finally:
        upstream.shutdown()
        upstream.server_close()

    assert response.status_code == 200
    assert _RecordingUpstream.received[0]["model"] == "glm-5.2"
    # 事件流仍保留客户端请求的原始模型名(可观测性如实呈现)
    requested = [data for event, data in observed if event == "proxy.requested"]
    assert requested[0]["model"] == "codex-auto-review"


def test_responses_without_upstream_model_passes_model_through():
    _RecordingUpstream.received = []
    upstream = HTTPServer(("127.0.0.1", 0), _RecordingUpstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    config = ProxyConfig(
        upstream_base=f"http://127.0.0.1:{upstream.server_address[1]}/v1",
        api_key="secret-upstream-key",
        local_token="local-token",
    )
    try:
        response = TestClient(create_app(config)).post(
            "/v1/responses",
            json={"model": "codex-auto-review", "input": "review this"},
            headers={"Authorization": "Bearer local-token"},
        )
    finally:
        upstream.shutdown()
        upstream.server_close()

    assert response.status_code == 200
    assert _RecordingUpstream.received[0]["model"] == "codex-auto-review"


# ---- 生命周期:启动超时 / 活动 SSE ----


def test_start_timeout_cleans_up_thread_and_socket(monkeypatch):
    # healthz 一直不通 → start 超时 → 必须清理线程与 socket,不留泄漏
    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    srv = ProxyServer(_cfg())
    with pytest.raises(RuntimeError):
        srv.start(wait_ready=0.3)
    assert srv._thread is None and srv._sock is None


class _HangSSEHandler(BaseHTTPRequestHandler):
    """一直发 SSE chunk 不结束的假上游(模拟活动 SSE)。"""

    accept: str | None = None

    def do_POST(self):
        type(self).accept = self.headers.get("Accept")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            while True:
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n')
                self.wfile.flush()
                time.sleep(0.02)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def log_message(self, *a):
        pass


def test_stop_with_active_sse_reclaims_thread(capfd):
    up = HTTPServer(("127.0.0.1", 0), _HangSSEHandler)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    cfg = ProxyConfig(
        upstream_base=f"http://127.0.0.1:{up.server_address[1]}/v1",
        api_key="sk",
        local_token="tok",
        timeout=30,
    )
    srv = ProxyServer(cfg)
    srv.start()
    stop_read = threading.Event()

    def _read():
        try:
            with httpx.stream(
                "POST",
                f"http://{srv.host}:{srv.port}/v1/responses",
                json={"model": "m", "input": "hi", "stream": True},
                headers={"Authorization": "Bearer tok"},
                timeout=10,
            ) as r:
                for _ in r.iter_lines():
                    if stop_read.is_set():
                        break
        except Exception:  # noqa: BLE001
            pass

    rt = threading.Thread(target=_read, daemon=True)
    rt.start()
    time.sleep(0.6)  # 让活动 SSE 建立
    thread = srv._thread  # stop 前保存引用(stop 会把 _thread 置 None)
    srv.stop()  # 活动 SSE 中 stop 必须中断并回收线程(泄漏回归)
    assert thread is not None and not thread.is_alive()
    stop_read.set()
    rt.join(timeout=3)
    up.shutdown()
    up.server_close()
    assert _HangSSEHandler.accept == "text/event-stream"
    captured = capfd.readouterr()
    assert "Traceback (most recent call last)" not in captured.err
    assert "asyncio.exceptions.CancelledError" not in captured.err


# ---- E2E: codex 动态工具(additional_tools/namespace)全链路工具调用往返 ----


class _ToolCallStreamingUpstream(BaseHTTPRequestHandler):
    """录制 chat 请求体并回放一条 tool_calls 流式响应(模拟 glm/kspmas 行为)。"""

    received: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or "0")
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).received.append(body)
        chunks = [
            {
                "id": "chatcmpl-tools",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": body.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "reasoning_content": "call exec"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-tools",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": body.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "id": "call_live_1",
                                    "index": 0,
                                    "type": "function",
                                    "function": {
                                        "name": "functions__exec_command",
                                        "arguments": '{"cmd": "echo kernel-ok"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-tools",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": body.get("model"),
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        ]
        payload = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        encoded = payload.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        pass


def test_tool_round_trip_through_streaming_proxy():
    """glm-5.3 经 model_proxy 无 tool_calls 的回归:工具声明与 tool_calls 双向不丢。

    codex 0.147 把动态工具放 input 的 additional_tools(namespace 包裹),
    转换层必须提升进 chat tools;上游回的 tool_calls 流必须还原成
    responses function_call 事件(namespace 名字还原),codex 才能执行工具。
    """
    _ToolCallStreamingUpstream.received = []
    upstream = HTTPServer(("127.0.0.1", 0), _ToolCallStreamingUpstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    config = ProxyConfig(
        upstream_base=f"http://127.0.0.1:{upstream.server_address[1]}/v1",
        api_key="secret-upstream-key",
        local_token="local-token",
        upstream_model="glm-5.3",
    )
    responses_body = {
        # 真实链路:codex 已配置 model=(或 thread 级覆盖),发真实模型名
        "model": "glm-5.3",
        "instructions": "You are a coding agent.",
        "input": [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "functions",
                        "tools": [
                            {
                                "type": "function",
                                "name": "exec_command",
                                "description": "Run a command",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"cmd": {"type": "string"}},
                                    "required": ["cmd"],
                                },
                            }
                        ],
                    }
                ],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "run echo kernel-ok"}],
            },
        ],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "reasoning": {"effort": "xhigh"},
        "stream": True,
    }
    try:
        with TestClient(create_app(config)) as client:
            response = client.post(
                "/v1/responses",
                json=responses_body,
                headers={"Authorization": "Bearer local-token"},
            )
            sse = response.text
    finally:
        upstream.shutdown()
        upstream.server_close()

    # 请求方向:chat 上游收到提升后的 tools / tool_choice / parallel_tool_calls
    chat_req = _ToolCallStreamingUpstream.received[0]
    tool_names = [
        (t.get("function") or {}).get("name") for t in chat_req.get("tools") or []
    ]
    assert tool_names == ["functions__exec_command"], "工具声明在转换中丢失(根因)"
    assert chat_req["tool_choice"] == "auto"
    assert chat_req["parallel_tool_calls"] is False
    assert chat_req["model"] == "glm-5.3"
    assert chat_req["stream"] is True
    # 响应方向:responses SSE 里有 function_call 事件且 namespace 名字还原
    assert "response.function_call_arguments.delta" in sse
    assert "response.function_call_arguments.done" in sse
    assert '"call_id": "call_live_1"' in sse
    # output_item.added 阶段用 flat 名字(与上游 chat 流一致);
    # output_item.done 是 namespace 还原后的终态,以它为准。
    done_items = []
    for line in sse.splitlines():
        if not line.startswith("data: "):
            continue
        data = json.loads(line[6:])
        if data.get("type") == "response.output_item.done" and (
            data.get("item") or {}
        ).get("type") == "function_call":
            done_items.append(data["item"])
    assert len(done_items) == 1
    item = done_items[0]
    assert item["name"] == "exec_command"
    assert item["namespace"] == "functions"
    assert item["call_id"] == "call_live_1"
    assert "response.completed" in sse


def test_responses_real_model_not_clobbered_by_upstream_default():
    """RunAgent 的 Model 覆盖必须透传:proxy 只对 codex 内部伪模型名(如
    codex-auto-review)落回 upstream_model,真实模型名原样发给上游。

    否则请求带 model=glm-5.3 会被改写成部署默认模型(终验 403 的根因之一)。
    """
    _RecordingUpstream.received = []
    upstream = HTTPServer(("127.0.0.1", 0), _RecordingUpstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    config = ProxyConfig(
        upstream_base=f"http://127.0.0.1:{upstream.server_address[1]}/v1",
        api_key="secret-upstream-key",
        local_token="local-token",
        upstream_model="gpt-5.6-sol",
    )
    try:
        response = TestClient(create_app(config)).post(
            "/v1/responses",
            json={"model": "glm-5.3", "input": "hello"},
            headers={"Authorization": "Bearer local-token"},
        )
    finally:
        upstream.shutdown()
        upstream.server_close()

    assert response.status_code == 200
    assert _RecordingUpstream.received[0]["model"] == "glm-5.3"
