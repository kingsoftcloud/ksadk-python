from __future__ import annotations

import socket

from ksadk.common.aicp_env import resolve_aicp_connection


def test_resolve_aicp_connection_uses_explicit_endpoint(monkeypatch):
    monkeypatch.setenv("KSADK_KB_ENDPOINT", "aicp.example.com")
    monkeypatch.setenv("KSADK_KB_REGION", "pre-online")

    connection = resolve_aicp_connection("KSADK_KB")

    assert connection == {
        "endpoint": "aicp.example.com",
        "scheme": "https",
        "region": "pre-online",
    }


def test_resolve_aicp_connection_prefers_reachable_inner_endpoint(monkeypatch):
    def fake_create_connection(address, timeout=1.0):
        host, port = address
        if host == "aicp.internal.api.ksyun.com" and port == 80:
            return _FakeSocket()
        raise OSError("unreachable")

    monkeypatch.delenv("KSADK_KB_ENDPOINT", raising=False)
    monkeypatch.delenv("KSADK_KB_SCHEME", raising=False)
    monkeypatch.setattr(socket, "create_connection", fake_create_connection)

    connection = resolve_aicp_connection("KSADK_KB")

    assert connection["endpoint"] == "aicp.internal.api.ksyun.com"
    assert connection["scheme"] == "http"


def test_resolve_aicp_connection_falls_back_to_inner_when_internal_unreachable(monkeypatch):
    def fake_create_connection(address, timeout=1.0):
        host, port = address
        if host == "aicp.inner.api.ksyun.com" and port == 80:
            return _FakeSocket()
        raise OSError("unreachable")

    monkeypatch.delenv("KSADK_KB_ENDPOINT", raising=False)
    monkeypatch.delenv("KSADK_KB_SCHEME", raising=False)
    monkeypatch.setattr(socket, "create_connection", fake_create_connection)

    connection = resolve_aicp_connection("KSADK_KB")

    assert connection["endpoint"] == "aicp.inner.api.ksyun.com"
    assert connection["scheme"] == "http"


def test_resolve_aicp_connection_falls_back_to_public_when_private_endpoints_unreachable(monkeypatch):
    monkeypatch.delenv("KSADK_KB_ENDPOINT", raising=False)
    monkeypatch.delenv("KSADK_KB_SCHEME", raising=False)
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))

    connection = resolve_aicp_connection("KSADK_KB")

    assert connection["endpoint"] == "aicp.api.ksyun.com"
    assert connection["scheme"] == "https"


def test_resolve_aicp_connection_honors_global_endpoint_mode(monkeypatch):
    monkeypatch.delenv("KSADK_KB_ENDPOINT", raising=False)
    monkeypatch.delenv("KSADK_KB_SCHEME", raising=False)
    monkeypatch.setenv("KSADK_AICP_ENDPOINT_MODE", "inner")
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))

    connection = resolve_aicp_connection("KSADK_KB")

    assert connection["endpoint"] == "aicp.inner.api.ksyun.com"
    assert connection["scheme"] == "http"


class _FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False
