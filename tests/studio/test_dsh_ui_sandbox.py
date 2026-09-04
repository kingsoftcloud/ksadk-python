from __future__ import annotations

import hashlib
import json

import pytest

from ksadk.studio.dsh_ui_sandbox import (
    DSH_UI_FRAME_ORIGIN,
    DshClientBundleExecution,
    DshUiSandboxError,
    DshUiSandboxLimits,
    DshUiSandboxSessionStore,
    dsh_ui_client_bundle_headers,
    legacy_top_level_client_execution_allowed,
    parse_dsh_ui_request,
    render_dsh_ui_sandbox_document,
    select_dsh_client_bundle_execution,
)

CLIENT = b"window.fixtureLoaded = true;\n"
DIGEST = f"sha256:{hashlib.sha256(CLIENT).hexdigest()}"
ORIGIN = "http://127.0.0.1:43210"
GENERATION_ID = "dshgen_" + "g" * 32


def _store_and_grant(
    *,
    limits: DshUiSandboxLimits | None = None,
    clock=None,
):
    kwargs = {"limits": limits}
    if clock is not None:
        kwargs["clock"] = clock
    store = DshUiSandboxSessionStore(**kwargs)
    grant = store.create_session(
        plugin_id="@example/safe-ui",
        extension_id="fixture.tab",
        client_digest=DIGEST,
        descriptor_digest=DIGEST,
        generation_id=GENERATION_ID,
        parent_origin=ORIGIN,
        allowed_tool_ids=("@example/read", "@example/cancel"),
        agent_id="fixture-agent",
    )
    return store, grant


def _message(grant, method: str, payload: dict, *, request_id: str = "req_1", **changes):
    value = {
        "protocolVersion": "agentkit.dsh-ui/v1",
        "kind": "request",
        "sessionId": grant.session_id,
        "capabilityToken": grant.capability_token,
        "sourceId": grant.source_id,
        "requestId": request_id,
        "method": method,
        "payload": payload,
    }
    value.update(changes)
    return value


def _authorize(store, grant, message):
    return store.authorize_message(
        message,
        parent_origin=ORIGIN,
        source_id=grant.source_id,
        frame_origin=DSH_UI_FRAME_ORIGIN,
    )


def _assert_code(code: str, callback) -> DshUiSandboxError:
    with pytest.raises(DshUiSandboxError) as captured:
        callback()
    assert captured.value.code == code
    return captured.value


def test_session_uses_random_secrets_but_frame_document_contains_no_capability() -> None:
    store, first = _store_and_grant()
    second = store.create_session(
        plugin_id="@example/safe-ui",
        extension_id="fixture.tab",
        client_digest=DIGEST,
        descriptor_digest=DIGEST,
        generation_id=GENERATION_ID,
        parent_origin=ORIGIN,
        allowed_tool_ids=(),
    )

    assert first.session_id != second.session_id
    assert first.capability_token != second.capability_token
    assert first.capability_token not in repr(first)
    assert first.host_handshake()["capabilityToken"] == first.capability_token

    bundle_url = (
        "/api/v1/plugin-ecosystems/dsh/client-bundle"
        f"?pluginName=%40example%2Fsafe-ui&digest={DIGEST}"
    )
    document = render_dsh_ui_sandbox_document(first, client_bundle_url=bundle_url)

    assert first.capability_token not in document.html
    assert "X-CSRF-Token" not in document.html
    assert "document.cookie" not in document.html
    assert document.iframe_attributes == {
        "sandbox": "allow-scripts",
        "referrerpolicy": "no-referrer",
        "credentialless": "",
    }
    assert "allow-same-origin" not in document.iframe_attributes["sandbox"]
    assert "connect-src 'none'" in document.content_security_policy
    assert "frame-ancestors 'self'" in document.content_security_policy
    assert "sandbox allow-scripts" in document.content_security_policy
    assert "'unsafe-eval'" not in document.content_security_policy
    assert "script-src 'self'" not in document.content_security_policy
    assert 'integrity="sha256-' in document.html
    assert "X-Frame-Options" not in document.response_headers
    assert document.response_headers["Cache-Control"] == "no-store"


def test_bundle_response_is_anonymous_immutable_and_digest_fenced() -> None:
    headers = dsh_ui_client_bundle_headers(DIGEST)

    assert headers["Access-Control-Allow-Origin"] == "*"
    assert headers["Cross-Origin-Resource-Policy"] == "cross-origin"
    assert "immutable" in headers["Cache-Control"]
    assert headers["ETag"] == f'"{DIGEST}"'
    assert not any("cookie" in key.lower() for key in headers)


@pytest.mark.parametrize(
    "url",
    [
        f"https://evil.example/client.js?digest={DIGEST}",
        f"//evil.example/client.js?digest={DIGEST}",
        f"/api/v1/plugin-ecosystems/dsh/client-bundle?digest={DIGEST}&token=secret",
        "/api/v1/plugin-ecosystems/dsh/client-bundle?digest=sha256:" + "0" * 64,
        f"/static/client.js?digest={DIGEST}",
    ],
)
def test_frame_rejects_unfenced_or_credential_bearing_bundle_url(url: str) -> None:
    _, grant = _store_and_grant()

    with pytest.raises(ValueError):
        render_dsh_ui_sandbox_document(grant, client_bundle_url=url)


def test_parser_accepts_only_three_typed_methods() -> None:
    _, grant = _store_and_grant()

    listed = parse_dsh_ui_request(json.dumps(_message(grant, "listTools", {})))
    called = parse_dsh_ui_request(
        _message(
            grant,
            "callTool",
            {
                "callId": "call_1",
                "toolId": "@example/read",
                "arguments": {"query": "safe"},
                "deadlineMs": 5000,
            },
        )
    )
    cancelled = parse_dsh_ui_request(_message(grant, "cancelTool", {"callId": "call_1"}))

    assert listed.method == "listTools"
    assert called.method == "callTool"
    assert called.payload.arguments == {"query": "safe"}
    assert cancelled.method == "cancelTool"

    for invalid in (
        _message(grant, "fetch", {"url": "https://evil.example"}),
        {**_message(grant, "listTools", {}), "csrfToken": "not-allowed"},
        _message(grant, "callTool", {"callId": "call_1", "toolId": "bad tool"}),
    ):
        _assert_code(
            "DSH_UI_MESSAGE_INVALID",
            lambda invalid=invalid: parse_dsh_ui_request(invalid),
        )


def test_parser_rejects_oversize_duplicate_and_non_finite_json() -> None:
    _, grant = _store_and_grant()
    valid = _message(grant, "listTools", {})

    _assert_code(
        "DSH_UI_MESSAGE_TOO_LARGE",
        lambda: parse_dsh_ui_request(valid, max_message_bytes=32),
    )
    duplicate = json.dumps(valid)[:-1] + ',"method":"cancelTool"}'
    _assert_code("DSH_UI_MESSAGE_INVALID", lambda: parse_dsh_ui_request(duplicate))
    non_finite = json.dumps(valid)[:-1] + ',"extra":NaN}'
    _assert_code("DSH_UI_MESSAGE_INVALID", lambda: parse_dsh_ui_request(non_finite))


@pytest.mark.parametrize(
    ("message_changes", "context_changes"),
    [
        ({"capabilityToken": "x" * 43}, {}),
        ({"sessionId": "dshui_" + "x" * 32}, {}),
        ({"sourceId": "frame_" + "x" * 24}, {}),
        ({}, {"source_id": "frame_" + "x" * 24}),
        ({}, {"parent_origin": "http://localhost:43210"}),
        ({}, {"frame_origin": ORIGIN}),
    ],
)
def test_authority_binds_token_session_origin_and_concrete_frame_source(
    message_changes: dict, context_changes: dict
) -> None:
    store, grant = _store_and_grant()
    context = {
        "parent_origin": ORIGIN,
        "source_id": grant.source_id,
        "frame_origin": DSH_UI_FRAME_ORIGIN,
    }
    context.update(context_changes)
    message = _message(grant, "listTools", {}, **message_changes)

    error = _assert_code(
        "DSH_UI_SESSION_INVALID",
        lambda: store.authorize_message(message, **context),
    )
    assert error.status_code == 403
    assert grant.capability_token not in str(error)


def test_authority_returns_only_session_scoped_tools_and_dispatch_data() -> None:
    store, grant = _store_and_grant()

    listed = _authorize(store, grant, _message(grant, "listTools", {}))
    called = _authorize(
        store,
        grant,
        _message(
            grant,
            "callTool",
            {
                "callId": "call_1",
                "toolId": "@example/read",
                "arguments": {"query": "safe"},
                "deadlineMs": 5000,
            },
            request_id="req_2",
        ),
    )

    assert listed.allowed_tool_ids == ("@example/cancel", "@example/read")
    assert called.plugin_id == "@example/safe-ui"
    assert called.agent_id == "fixture-agent"
    assert called.tool_id == "@example/read"
    assert called.call_id == "call_1"
    assert called.arguments == {"query": "safe"}
    assert not hasattr(called, "capability_token")

    _assert_code(
        "DSH_UI_TOOL_FORBIDDEN",
        lambda: _authorize(
            store,
            grant,
            _message(
                grant,
                "callTool",
                {
                    "callId": "call_2",
                    "toolId": "@evil/write",
                    "arguments": {},
                    "deadlineMs": 5000,
                },
                request_id="req_3",
            ),
        ),
    )


def test_concurrency_duplicate_cancel_and_completion_are_enforced() -> None:
    limits = DshUiSandboxLimits(max_concurrent_calls=1)
    store, grant = _store_and_grant(limits=limits)
    first = _message(
        grant,
        "callTool",
        {
            "callId": "call_1",
            "toolId": "@example/read",
            "arguments": {},
            "deadlineMs": 5000,
        },
    )
    _authorize(store, grant, first)

    _assert_code(
        "DSH_UI_CALL_DUPLICATE",
        lambda: _authorize(store, grant, {**first, "requestId": "req_2"}),
    )
    second = _message(
        grant,
        "callTool",
        {
            "callId": "call_2",
            "toolId": "@example/read",
            "arguments": {},
            "deadlineMs": 5000,
        },
        request_id="req_3",
    )
    _assert_code("DSH_UI_CALL_LIMIT_REACHED", lambda: _authorize(store, grant, second))

    cancel = _authorize(
        store,
        grant,
        _message(
            grant,
            "cancelTool",
            {"callId": "call_1"},
            request_id="req_cancel",
        ),
    )
    assert cancel.call_id == "call_1"
    assert store.complete_call(grant.session_id, "call_1") is True
    assert store.complete_call(grant.session_id, "call_1") is False
    _authorize(store, grant, second)


def test_rate_limit_does_not_prevent_cancellation() -> None:
    limits = DshUiSandboxLimits(max_requests_per_window=1, max_concurrent_calls=1)
    store, grant = _store_and_grant(limits=limits)
    _authorize(
        store,
        grant,
        _message(
            grant,
            "callTool",
            {
                "callId": "call_1",
                "toolId": "@example/read",
                "arguments": {},
                "deadlineMs": 5000,
            },
        ),
    )

    _assert_code(
        "DSH_UI_RATE_LIMITED",
        lambda: _authorize(
            store,
            grant,
            _message(grant, "listTools", {}, request_id="req_rate"),
        ),
    )
    cancelled = _authorize(
        store,
        grant,
        _message(grant, "cancelTool", {"callId": "call_1"}, request_id="req_cancel"),
    )
    assert cancelled.method == "cancelTool"


def test_expiry_and_plugin_revocation_return_calls_for_host_cancellation() -> None:
    now = [100.0]
    limits = DshUiSandboxLimits(session_ttl_seconds=10, idle_ttl_seconds=5)
    store, grant = _store_and_grant(limits=limits, clock=lambda: now[0])
    _authorize(
        store,
        grant,
        _message(
            grant,
            "callTool",
            {
                "callId": "call_1",
                "toolId": "@example/read",
                "arguments": {},
                "deadlineMs": 5000,
            },
        ),
    )

    revoked = store.revoke_plugin("@example/safe-ui")
    assert revoked == {grant.session_id: ("call_1",)}
    _assert_code(
        "DSH_UI_SESSION_INVALID",
        lambda: _authorize(store, grant, _message(grant, "listTools", {}, request_id="req_2")),
    )

    store, grant = _store_and_grant(limits=limits, clock=lambda: now[0])
    _authorize(
        store,
        grant,
        _message(
            grant,
            "callTool",
            {
                "callId": "call_expiring",
                "toolId": "@example/read",
                "arguments": {},
                "deadlineMs": 5000,
            },
        ),
    )
    now[0] += 6
    assert store.purge_expired() == {grant.session_id: ("call_expiring",)}


def test_authorization_expiry_queues_active_call_for_the_sweeper() -> None:
    now = [100.0]
    limits = DshUiSandboxLimits(session_ttl_seconds=10, idle_ttl_seconds=5)
    store, grant = _store_and_grant(limits=limits, clock=lambda: now[0])
    _authorize(
        store,
        grant,
        _message(
            grant,
            "callTool",
            {
                "callId": "call_expiring",
                "toolId": "@example/read",
                "arguments": {},
                "deadlineMs": 5000,
            },
        ),
    )
    now[0] += 6

    _assert_code(
        "DSH_UI_SESSION_INVALID",
        lambda: _authorize(store, grant, _message(grant, "listTools", {}, request_id="req_2")),
    )
    assert store.purge_expired() == {grant.session_id: ("call_expiring",)}


def test_legacy_top_level_execution_is_default_deny_and_requires_explicit_gate() -> None:
    assert legacy_top_level_client_execution_allowed() is False
    assert legacy_top_level_client_execution_allowed(explicit_opt_in=True) is True
    assert (
        select_dsh_client_bundle_execution(sandbox_compatible=True)
        == DshClientBundleExecution.SANDBOX
    )
    assert (
        select_dsh_client_bundle_execution(sandbox_compatible=False)
        == DshClientBundleExecution.DENY
    )
    assert (
        select_dsh_client_bundle_execution(
            sandbox_compatible=False,
            legacy_compatible=True,
            explicit_legacy_opt_in=True,
        )
        == DshClientBundleExecution.LEGACY_TOP_LEVEL
    )
    assert (
        select_dsh_client_bundle_execution(
            sandbox_compatible=False,
            legacy_compatible=False,
            explicit_legacy_opt_in=True,
        )
        == DshClientBundleExecution.DENY
    )
