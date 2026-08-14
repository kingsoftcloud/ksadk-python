from __future__ import annotations

import socket
from unittest.mock import Mock

import httpx

from ksadk.toolsets import a2a


def test_call_a2a_agent_rejects_card_hostname_resolving_to_private_address(monkeypatch):
    monkeypatch.setenv("KSADK_A2A_SPACE_ID", "space-1")
    monkeypatch.setattr(
        a2a,
        "_discover_agents",
        lambda: [{"agent_id": "agent-1", "name": "agent", "url": "https://agent.example"}],
    )

    def private_addresses(*_args, **_kwargs):
        return [(None, None, None, None, ("127.0.0.1", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", private_addresses)
    monkeypatch.setattr(httpx, "Client", Mock(side_effect=AssertionError))

    result = a2a.call_a2a_agent("agent", "hello")

    assert result["ok"] is False
    assert result["error_type"] == "blocked_by_ssrf_policy"


def test_validate_card_url_rejects_password_only_userinfo():
    result = a2a._validate_card_url("https://:password@agent.example")

    assert result is not None
    assert result["error_type"] == "invalid_card_url"


def test_validate_card_url_rejects_non_global_address(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("100.64.0.1", 443))],
    )

    result = a2a._validate_card_url("https://agent.example")

    assert result is not None
    assert result["error_type"] == "blocked_by_ssrf_policy"


def test_pinned_backend_dials_validated_ip_and_preserves_expected_origin():
    class RecordingBackend:
        def __init__(self):
            self.calls = []

        def connect_tcp(self, host, port, **kwargs):
            self.calls.append((host, port, kwargs))
            return object()

        def connect_unix_socket(self, path, **kwargs):
            raise AssertionError(path)

        def sleep(self, seconds):
            return None

    delegate = RecordingBackend()
    backend = a2a._PinnedDNSNetworkBackend(
        delegate,
        expected_hostname="agent.example",
        expected_port=443,
        pinned_ip="93.184.216.34",
    )

    backend.connect_tcp("agent.example", 443, timeout=5)

    assert delegate.calls == [
        ("93.184.216.34", 443, {"timeout": 5, "local_address": None, "socket_options": None})
    ]
