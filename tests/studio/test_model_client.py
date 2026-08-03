from __future__ import annotations

import socket

import httpx
import pytest

from ksadk.studio.contracts import ModelParameters, NetworkPolicy, ResolvedModel
from ksadk.studio.errors import StudioError
from ksadk.studio.model_client import (
    CredentialResolver,
    NetworkGuard,
    OpenAICompatibleModelClient,
)


class AllowNetwork:
    async def check(self, _endpoint_url, _policy):
        return None


def _model() -> ResolvedModel:
    return ResolvedModel(
        provider="openai-compatible",
        model="glm-5.1",
        endpoint_url="https://model.example.com/v1/chat/completions",
        credential_ref="env://MODEL_API_KEY",
        parameters=ModelParameters(temperature=0.1, max_tokens=64),
    )


@pytest.mark.asyncio
async def test_model_client_sends_openai_compatible_request(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        captured["json"] = __import__("json").loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 1,
                    "total_tokens": 4,
                },
            },
        )

    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
    )
    result = await client.complete(
        _model(),
        messages=[{"role": "user", "content": "test"}],
        network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
    )

    assert captured["authorization"] == "Bearer secret-value"
    assert captured["json"]["model"] == "glm-5.1"
    assert captured["json"]["stream"] is False
    assert result.content == "OK"
    assert result.usage.total_tokens == 4


@pytest.mark.asyncio
async def test_model_client_retries_5xx_without_leaking_secret(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "top-secret")
    attempts = 0
    sleeps = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, text="upstream failed")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "OK"}, "finish_reason": "stop"}
                ]
            },
        )

    async def fake_sleep(value):
        sleeps.append(value)

    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
        sleep=fake_sleep,
    )
    result = await client.complete(
        _model(),
        messages=[],
        network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
        timeout_seconds=10,
        max_attempts=2,
        backoff_seconds=0.5,
    )

    assert result.content == "OK"
    assert attempts == 2
    assert sleeps == [0.5]


@pytest.mark.asyncio
async def test_model_client_rejects_redirect(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                302, headers={"Location": "https://attacker.invalid"}
            )
        ),
    )

    with pytest.raises(StudioError) as captured:
        await client.complete(
            _model(),
            messages=[],
            network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
            timeout_seconds=10,
            max_attempts=1,
            backoff_seconds=0,
        )

    assert captured.value.code == "NETWORK_TARGET_DENIED"
    assert "secret-value" not in str(captured.value.as_dict())


@pytest.mark.asyncio
async def test_model_client_rejects_reasoning_only_length_response(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "reasoning_content": "internal reasoning",
                            },
                            "finish_reason": "length",
                        }
                    ],
                    "usage": {"total_tokens": 64},
                },
            )
        ),
    )

    with pytest.raises(StudioError) as captured:
        await client.complete(
            _model(),
            messages=[],
            network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
            timeout_seconds=10,
            max_attempts=1,
            backoff_seconds=0,
        )

    assert captured.value.code == "MODEL_EMPTY_RESPONSE"
    assert captured.value.details == {"finishReason": "length"}


def test_credential_resolver_never_accepts_plaintext_or_missing_env(monkeypatch):
    resolver = CredentialResolver()
    monkeypatch.delenv("MODEL_API_KEY", raising=False)

    with pytest.raises(StudioError) as missing:
        resolver.resolve("env://MODEL_API_KEY")
    assert missing.value.code == "SECRET_NOT_FOUND"

    with pytest.raises(StudioError) as invalid:
        resolver.resolve("plain-secret")
    assert invalid.value.code == "SECRET_BACKEND_UNAVAILABLE"


def test_credential_resolver_session_overlay_precedes_environment(monkeypatch):
    resolver = CredentialResolver()
    monkeypatch.setenv("MODEL_API_KEY", "environment-secret")

    assert resolver.status("env://MODEL_API_KEY") == {
        "reference": "env://MODEL_API_KEY",
        "name": "MODEL_API_KEY",
        "configured": True,
        "source": "environment",
        "persistence": "environment",
    }

    configured = resolver.put_session("MODEL_API_KEY", "session-secret")
    assert configured["source"] == "session"
    assert resolver.resolve("env://MODEL_API_KEY") == "session-secret"
    assert "session-secret" not in str(configured)

    restored = resolver.delete_session("MODEL_API_KEY")
    assert restored["source"] == "environment"
    assert resolver.resolve("env://MODEL_API_KEY") == "environment-secret"


def test_credential_resolver_rejects_unsafe_session_values():
    resolver = CredentialResolver()

    for value in {"", " leading", "trailing ", "line\nbreak"}:
        with pytest.raises(StudioError) as captured:
            resolver.put_session("MODEL_API_KEY", value)
        assert captured.value.code == "SECRET_VALUE_INVALID"

    with pytest.raises(StudioError) as invalid_name:
        resolver.put_session("lowercase-key", "secret")
    assert invalid_name.value.code == "SECRET_REFERENCE_INVALID"


def test_credential_resolver_clears_all_session_values(monkeypatch):
    resolver = CredentialResolver()
    monkeypatch.delenv("FIRST_KEY", raising=False)
    monkeypatch.delenv("SECOND_KEY", raising=False)
    resolver.put_session("FIRST_KEY", "first-secret")
    resolver.put_session("SECOND_KEY", "second-secret")

    resolver.clear_session()

    assert resolver.status("env://FIRST_KEY")["configured"] is False
    assert resolver.status("env://SECOND_KEY")["configured"] is False


@pytest.mark.asyncio
async def test_network_guard_denies_metadata_and_private_dns(monkeypatch):
    guard = NetworkGuard()
    policy = NetworkPolicy(allowed_hosts=["model.example.com"])

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 443))
        ],
    )
    with pytest.raises(StudioError) as private:
        await guard.check(
            "https://model.example.com/v1/chat/completions",
            policy,
        )
    assert private.value.code == "NETWORK_TARGET_DENIED"

    with pytest.raises(StudioError) as metadata:
        await guard.check(
            "http://169.254.169.254/latest/meta-data",
            NetworkPolicy(
                allowed_hosts=["169.254.169.254"],
                allow_private_network=True,
            ),
        )
    assert metadata.value.code == "NETWORK_TARGET_DENIED"


@pytest.mark.asyncio
async def test_network_guard_allows_explicit_private_target(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8000))
        ],
    )

    await NetworkGuard().check(
        "http://localhost:8000/v1/chat/completions",
        NetworkPolicy(
            allowed_hosts=["localhost"],
            allow_private_network=True,
        ),
    )
