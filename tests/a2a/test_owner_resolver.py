# -*- coding: utf-8 -*-
"""default_owner_resolver 组合 account+runtime identity 的测试(§4.6,review 修复)。

owner 必须是 account 与 runtime 的组合,使同 account 不同 runtime 形成独立任务权限域;
不再是"取第一个存在字段"。
"""

from __future__ import annotations

from starlette.requests import Request

from ksadk.a2a.task_store import A2AOwnerContextBuilder, default_owner_resolver


class _Ctx:
    def __init__(self, state: dict) -> None:
        self.state = state


def test_owner_composes_account_and_runtime() -> None:
    owner = default_owner_resolver(_Ctx({"account_id": "acc1", "runtime_id": "rt1"}))  # type: ignore[arg-type]
    assert owner == "acc1/rt1"


def test_same_account_different_runtime_are_distinct() -> None:
    a = default_owner_resolver(_Ctx({"account_id": "acc1", "runtime_id": "rt1"}))  # type: ignore[arg-type]
    b = default_owner_resolver(_Ctx({"account_id": "acc1", "runtime_id": "rt2"}))  # type: ignore[arg-type]
    assert a != b
    assert a == "acc1/rt1" and b == "acc1/rt2"


def test_tenant_and_agent_fallback() -> None:
    owner = default_owner_resolver(_Ctx({"tenant_id": "t1", "agent_id": "ag1"}))  # type: ignore[arg-type]
    assert owner == "t1/ag1"


def test_only_account_side_used_when_runtime_missing() -> None:
    assert default_owner_resolver(_Ctx({"account_id": "acc1"})) == "acc1"  # type: ignore[arg-type]


def test_only_runtime_side_used_when_account_missing() -> None:
    assert default_owner_resolver(_Ctx({"runtime_id": "rt1"})) == "rt1"  # type: ignore[arg-type]


def test_falls_back_to_user_then_anonymous() -> None:
    assert default_owner_resolver(_Ctx({"user": "u1"})) == "u1"  # type: ignore[arg-type]
    assert default_owner_resolver(_Ctx({})) == "anonymous"  # type: ignore[arg-type]
    assert default_owner_resolver(_Ctx({"account_id": "", "runtime_id": None})) == "anonymous"  # type: ignore[arg-type]


def test_http_owner_builder_normalizes_trusted_identity_for_all_transports() -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/a2a/v1/jsonrpc",
            "headers": [
                (b"x-ksc-account-id", b"acc-http"),
                (b"x-auth-agent-id", b"runtime-http"),
            ],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1),
        }
    )
    context = A2AOwnerContextBuilder().build(request)
    assert context.state["account_id"] == "acc-http"
    assert context.state["runtime_id"] == "runtime-http"
    assert context.tenant == "acc-http/runtime-http"
