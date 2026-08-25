from __future__ import annotations

import socket
from pathlib import Path

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
            json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]},
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
            lambda _request: httpx.Response(302, headers={"Location": "https://attacker.invalid"})
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


def test_credential_resolver_applies_alias_to_workspace_secret(tmp_path: Path):
    """Break caught: Studio sees a persisted model key but Codex receives no auth."""

    from ksadk.studio.workspace import Workspace

    workspace = Workspace(tmp_path)
    workspace.initialize()
    resolver = CredentialResolver(workspace)
    resolver.put_session("AGENTKIT_MODEL_API_KEY", "workspace-model-key")
    resolver = CredentialResolver(workspace)

    assert resolver.resolve("env://OPENAI_API_KEY") == "workspace-model-key"
    status = resolver.status("env://OPENAI_API_KEY")
    assert status["configured"] is True
    assert status["source"] == "workspace-alias"


def test_credential_resolver_prefers_workspace_alias_over_process_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A host OPENAI_API_KEY must not shadow a Studio-configured model credential."""

    from ksadk.studio.workspace import Workspace

    monkeypatch.setenv("OPENAI_API_KEY", "host-primary-key")
    workspace = Workspace(tmp_path)
    workspace.initialize()
    workspace.atomic_write_text(
        ".agentkit/secrets.env",
        "AGENTKIT_MODEL_API_KEY=workspace-profile-key\n",
    )

    resolver = CredentialResolver(workspace)

    assert resolver.resolve("env://OPENAI_API_KEY") == "workspace-profile-key"


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
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 443))],
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


@pytest.mark.asyncio
async def test_model_client_sends_response_format_when_allowed(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(__import__("json").loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]},
        )

    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
    )
    await client.complete(
        _model(),
        messages=[{"role": "user", "content": "compose"}],
        network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
        response_format={"type": "json_object"},
    )

    assert captured[0]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_model_client_omits_response_format_when_disabled(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(__import__("json").loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]},
        )

    model = _model()
    model.parameters = model.parameters.model_copy(update={"allow_json_response_format": False})
    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
    )
    await client.complete(
        model,
        messages=[],
        network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
        response_format={"type": "json_object"},
    )

    assert "response_format" not in captured[0]


@pytest.mark.asyncio
async def test_model_client_retries_400_without_response_format(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        captured.append(payload)
        if "response_format" in payload:
            return httpx.Response(
                400,
                json={"error": {"message": "response_format is not supported"}},
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]},
        )

    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
    )
    result = await client.complete(
        _model(),
        messages=[],
        network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
        response_format={"type": "json_object"},
    )

    assert result.content == "{}"
    assert len(captured) == 2
    assert "response_format" in captured[0]
    assert "response_format" not in captured[1]


@pytest.mark.asyncio
async def test_model_client_retries_length_truncation_with_larger_budget(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": ""},
                            "finish_reason": "length",
                        }
                    ],
                    "usage": {"total_tokens": 64},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": '{"ok": true}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"total_tokens": 128},
            },
        )

    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
    )
    result = await client.complete(
        _model(),
        messages=[],
        network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
        retry_on_length=True,
    )

    assert result.content == '{"ok": true}'
    assert len(requests) == 2
    assert requests[0]["max_tokens"] == 64
    assert requests[1]["max_tokens"] == 16384


@pytest.mark.asyncio
async def test_model_client_length_retry_exhausted_raises_empty_response(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": ""},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"total_tokens": 64},
            },
        )

    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(StudioError) as captured:
        await client.complete(
            _model(),
            messages=[],
            network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
            timeout_seconds=10,
            max_attempts=1,
            backoff_seconds=0,
            retry_on_length=True,
        )
    assert captured.value.code == "MODEL_EMPTY_RESPONSE"
    assert captured.value.details == {"finishReason": "length"}


@pytest.mark.asyncio
async def test_model_client_no_length_retry_by_default(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    calls = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": ""},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"total_tokens": 64},
            },
        )

    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(StudioError) as captured:
        await client.complete(
            _model(),
            messages=[],
            network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
            timeout_seconds=10,
            max_attempts=2,
            backoff_seconds=0,
        )
    assert captured.value.code == "MODEL_EMPTY_RESPONSE"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_model_client_omits_unset_sampling_parameters(monkeypatch):
    """未显式配置的 temperature/max_tokens/top_p 一律不出现在 payload。"""

    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = __import__("json").loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}
                ]
            },
        )

    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
    )
    await client.complete(
        ResolvedModel(
            provider="openai-compatible",
            model="kimi-k2",
            endpoint_url="https://model.example.com/v1/chat/completions",
            credential_ref="env://MODEL_API_KEY",
            parameters=ModelParameters(),
        ),
        messages=[],
        network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
    )

    body = captured["json"]
    assert "temperature" not in body
    assert "max_tokens" not in body
    assert "top_p" not in body


@pytest.mark.asyncio
async def test_model_client_sends_explicitly_configured_sampling_parameters(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "secret-value")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = __import__("json").loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}
                ]
            },
        )

    client = OpenAICompatibleModelClient(
        network_guard=AllowNetwork(),
        transport=httpx.MockTransport(handler),
    )
    await client.complete(
        ResolvedModel(
            provider="openai-compatible",
            model="glm-5-3",
            endpoint_url="https://model.example.com/v1/chat/completions",
            credential_ref="env://MODEL_API_KEY",
            parameters=ModelParameters(temperature=0.7, max_tokens=8192, top_p=0.9),
        ),
        messages=[],
        network_policy=NetworkPolicy(allowed_hosts=["model.example.com"]),
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
    )

    body = captured["json"]
    assert body["temperature"] == 0.7
    assert body["max_tokens"] == 8192
    assert body["top_p"] == 0.9
