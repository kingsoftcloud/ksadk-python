"""client.py X-Ksc-User-uuid header 注入测试。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ksadk.api.client import AgentEngineClient
from ksadk.identity.resolver import ResolvedIdentity


@pytest.fixture(autouse=True)
def _isolate_identity_env(monkeypatch):
    """每个测试隔离 env + 缓存，避免互相影响。"""
    monkeypatch.delenv("KSYUN_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("KSYUN_ACCESS_KEY", raising=False)
    monkeypatch.delenv("KSYUN_SECRET_KEY", raising=False)
    # 隔离缓存（patch resolve_identity/get_cached_identity 避免真实文件读写）
    yield


def _make_client_with_creds(monkeypatch, *, access_key="AKLTtest", secret_key="SKtest", dry_run=False):
    """构造带凭证的 client，绕过真实 AK/SK env 依赖。"""
    client = AgentEngineClient(region="cn-beijing-6", dry_run=dry_run)
    # 注入凭证到 _auth
    client._auth.access_key_id = access_key
    client._auth.secret_access_key = secret_key
    return client


def test_build_headers_no_user_uuid_when_resolve_fails(monkeypatch):
    """反查失败时不注入 X-Ksc-User-uuid，其他 header 正常。"""
    client = _make_client_with_creds(monkeypatch)
    monkeypatch.setattr("ksadk.identity.resolve_identity", lambda **kw: None)

    headers = client._build_headers(action="Test")

    assert "X-Ksc-User-uuid" not in headers
    assert headers["X-Ksc-Source"] == "ksadk-cli"
    assert "X-Ksc-Region" in headers


def test_build_headers_includes_user_uuid_after_resolve(monkeypatch):
    """反查成功时注入 X-Ksc-User-uuid。"""
    client = _make_client_with_creds(monkeypatch)
    fake = ResolvedIdentity(
        user_uuid="uuid-xyz",
        main_account_id="2000003485",
        user_name="xiayu",
        krn="krn:ksc:iam::2000003485:user/xiayu",
        ak_fingerprint="abc",
    )
    monkeypatch.setattr("ksadk.identity.resolve_identity", lambda **kw: fake)

    headers = client._build_headers(action="Test")

    assert headers["X-Ksc-User-uuid"] == "uuid-xyz"
    # account_id 也从反查拿到
    assert headers["X-Ksc-Account-Id"] == "2000003485"


def test_build_headers_extra_headers_override_user_uuid(monkeypatch):
    """extra_headers 显式覆盖 user uuid。"""
    client = _make_client_with_creds(monkeypatch)
    client.extra_headers = {"X-Ksc-User-uuid": "custom-uuid", "X-Ksc-Account-Id": "custom-acct"}
    monkeypatch.setattr("ksadk.identity.resolve_identity", lambda **kw: MagicMock(user_uuid="should-not-use"))

    headers = client._build_headers(action="Test")

    # extra_headers 在 _resolve_user_uuid 里优先返回，且 _build_headers 末尾 update 覆盖
    assert headers["X-Ksc-User-uuid"] == "custom-uuid"
    assert headers["X-Ksc-Account-Id"] == "custom-acct"


def test_build_headers_lowercase_extra_headers_normalized(monkeypatch):
    """extra_headers 用小写 key 时归一为 Title-Case，避免重复 header。"""
    client = _make_client_with_creds(monkeypatch)
    client.extra_headers = {"x-ksc-user-uuid": "custom", "x-ksc-account-id": "custom-acct"}
    monkeypatch.setattr("ksadk.identity.resolve_identity", lambda **kw: MagicMock(user_uuid="should-not-use"))

    headers = client._build_headers(action="Test")

    # 只应有一个 X-Ksc-User-uuid（Title-Case），不应有小写 key 共存
    uuid_keys = [k for k in headers if k.lower() == "x-ksc-user-uuid"]
    assert len(uuid_keys) == 1
    assert uuid_keys[0] == "X-Ksc-User-uuid"
    assert headers["X-Ksc-User-uuid"] == "custom"
    acct_keys = [k for k in headers if k.lower() == "x-ksc-account-id"]
    assert len(acct_keys) == 1
    assert headers["X-Ksc-Account-Id"] == "custom-acct"


def test_dry_run_does_not_invoke_resolve(monkeypatch):
    """dry-run 不调 resolve_identity，只读缓存。"""
    client = _make_client_with_creds(monkeypatch, dry_run=True)
    called = MagicMock()
    monkeypatch.setattr("ksadk.identity.resolve_identity", lambda **kw: called())
    monkeypatch.setattr("ksadk.identity.get_cached_identity", lambda ak: None)

    client._build_headers(action="Test")

    assert called.call_count == 0  # dry-run 不联网反查


def test_dry_run_uses_cached_identity(monkeypatch):
    """dry-run 时从缓存读 identity 注入 header。"""
    client = _make_client_with_creds(monkeypatch, dry_run=True)
    fake = ResolvedIdentity(
        user_uuid="cached-uuid",
        main_account_id="2000003485",
        user_name="u",
        krn=None,
        ak_fingerprint="abc",
    )
    monkeypatch.setattr("ksadk.identity.resolve_identity", lambda **kw: None)
    monkeypatch.setattr("ksadk.identity.get_cached_identity", lambda ak: fake)

    headers = client._build_headers(action="Test")

    assert headers["X-Ksc-User-uuid"] == "cached-uuid"


def test_resolve_user_uuid_cached_on_instance(monkeypatch):
    """同一 client 多次 _build_headers 只反查一次。"""
    client = _make_client_with_creds(monkeypatch)
    call_count = {"n": 0}

    def fake_resolve(**kw):
        call_count["n"] += 1
        return ResolvedIdentity(
            user_uuid="uuid-x", main_account_id=None, user_name="u", krn=None, ak_fingerprint="abc"
        )

    monkeypatch.setattr("ksadk.identity.resolve_identity", fake_resolve)

    client._build_headers(action="Test1")
    client._build_headers(action="Test2")
    client._build_headers(action="Test3")

    assert call_count["n"] == 1  # 实例缓存，只调一次


def test_account_id_env_overrides_resolve(monkeypatch):
    """KSYUN_ACCOUNT_ID env 优先于反查。"""
    monkeypatch.setenv("KSYUN_ACCOUNT_ID", "env-acct")
    client = _make_client_with_creds(monkeypatch)
    fake = ResolvedIdentity(
        user_uuid="uuid-x", main_account_id="resolved-acct", user_name="u", krn=None, ak_fingerprint="abc"
    )
    monkeypatch.setattr("ksadk.identity.resolve_identity", lambda **kw: fake)

    headers = client._build_headers(action="Test")

    assert headers["X-Ksc-Account-Id"] == "env-acct"  # env 覆盖反查


def test_account_id_falls_back_to_resolved_main_account(monkeypatch):
    """无 env 时 X-Ksc-Account-Id 从反查 main_account_id 拿。"""
    client = _make_client_with_creds(monkeypatch)
    fake = ResolvedIdentity(
        user_uuid="uuid-x", main_account_id="2000003485", user_name="u", krn=None, ak_fingerprint="abc"
    )
    monkeypatch.setattr("ksadk.identity.resolve_identity", lambda **kw: fake)

    headers = client._build_headers(action="Test")

    assert headers["X-Ksc-Account-Id"] == "2000003485"


def test_no_credentials_no_user_uuid_no_account_id(monkeypatch):
    """无 AK/SK 时 user_uuid/account_id 都为 None，不注入，不报错。"""
    client = AgentEngineClient(region="cn-beijing-6")  # 无凭证
    monkeypatch.setattr("ksadk.identity.resolve_identity", lambda **kw: None)

    headers = client._build_headers(action="Test")

    assert "X-Ksc-User-uuid" not in headers
    assert "X-Ksc-Account-Id" not in headers
