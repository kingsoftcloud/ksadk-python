"""server 层测试:create_app 鉴权 / 不支持工具 400 / ProxyServer 生命周期(含泄漏回归)。

均不打真实上游(鉴权与 400 在接触上游前返回;ProxyServer 用 /healthz,不触上游)。
"""

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
    assert r.json()["error"]["type"] == "unsupported_tools"


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

    def do_POST(self):
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


def test_stop_with_active_sse_reclaims_thread():
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
    srv.stop()  # 活动 SSE 中 stop:force_exit 必须中断并回收线程(泄漏回归)
    assert thread is not None and not thread.is_alive()
    stop_read.set()
    rt.join(timeout=3)
    up.shutdown()
    up.server_close()
