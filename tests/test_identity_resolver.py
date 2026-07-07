"""identity resolver 单测：AK/SK 反查 + 缓存 + 内网 fallback。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ksadk.identity.resolver import (
    ResolvedIdentity,
    _ak_fingerprint,
    _extract_main_account_id_from_krn,
    _find_username_by_ak,
    _resolve_iam_endpoint,
    _should_retry_intranet,
    get_cached_identity,
    get_cached_user_uuid,
    invalidate_cache,
    resolve_identity,
)


# ---------------------------------------------------------------------------
# 纯函数测试
# ---------------------------------------------------------------------------


def test_ak_fingerprint_stable_and_unique():
    fp1 = _ak_fingerprint("AKLTtest123")
    fp2 = _ak_fingerprint("AKLTtest123")
    fp3 = _ak_fingerprint("AKLTtest456")
    assert fp1 == fp2  # 稳定
    assert fp1 != fp3  # 不同 AK 不同指纹
    assert len(fp1) == 16


@pytest.mark.parametrize(
    "krn,expected",
    [
        ("krn:ksc:iam::2000003485:user/xiayu", "2000003485"),
        ("krn:ksc:iam::73398439:user/w_test", "73398439"),
        ("not a krn", None),
        ("", None),
        (None, None),
        ("krn:ksc:iam:::user/x", None),  # 空主账号 ID
    ],
)
def test_extract_main_account_id_from_krn(krn, expected):
    assert _extract_main_account_id_from_krn(krn) == expected


def test_find_username_by_ak():
    keys = [
        {"AccessKey": "AKLTaaa", "UserName": "user1"},
        {"AccessKey": "AKLTbbb", "UserName": "user2"},
    ]
    assert _find_username_by_ak(keys, "AKLTbbb") == "user2"
    assert _find_username_by_ak(keys, "AKLTccc") is None  # 不在列表（主账号 AK）
    assert _find_username_by_ak([], "AKLTaaa") is None


def test_resolve_iam_endpoint_default():
    old = (os.environ.get("KSYUN_IAM_URL"), os.environ.get("IAM_URL"))
    os.environ.pop("KSYUN_IAM_URL", None)
    os.environ.pop("IAM_URL", None)
    try:
        assert _resolve_iam_endpoint() == ("iam.api.ksyun.com", "https")
    finally:
        for k, v in zip(("KSYUN_IAM_URL", "IAM_URL"), old):
            if v is not None:
                os.environ[k] = v


def test_resolve_iam_endpoint_from_env():
    old = os.environ.get("KSYUN_IAM_URL")
    os.environ["KSYUN_IAM_URL"] = "http://iam.inner.api.ksyun.com"
    try:
        assert _resolve_iam_endpoint() == ("iam.inner.api.ksyun.com", "http")
    finally:
        if old is None:
            os.environ.pop("KSYUN_IAM_URL", None)
        else:
            os.environ["KSYUN_IAM_URL"] = old


def test_resolve_identity_intranet_fallback_requires_explicit_endpoint(isolated_cache, monkeypatch):
    """内网 IAM fallback 只能由显式环境变量启用，避免公开包硬编码内部 endpoint。"""
    sdk_parts = _mock_sdk_parts()
    IamClient = sdk_parts[0]
    client = MagicMock()
    IamClient.return_value = client
    client.ListAllUserAccessKeys.side_effect = Exception(
        '{"Error":{"Code":"InnerAccountCanOnlyAccessThroughIntranet"}}'
    )
    monkeypatch.delenv("KSYUN_IAM_INTRANET_URL", raising=False)
    monkeypatch.delenv("IAM_INTRANET_URL", raising=False)
    monkeypatch.setattr("ksadk.identity.resolver._import_iam_sdk", lambda: sdk_parts)

    assert resolve_identity(access_key="AKLTtest", secret_key="SKtest") is None
    assert client.ListAllUserAccessKeys.call_count == 1


def test_should_retry_intranet():
    exc = Exception('{"Error":{"Code":"InnerAccountCanOnlyAccessThroughIntranet"}}')
    assert _should_retry_intranet(exc) is True
    assert _should_retry_intranet(Exception("other error")) is False
    assert _should_retry_intranet(None) is False


# ---------------------------------------------------------------------------
# 缓存测试（monkeypatch 缓存路径到 tmp_path）
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    """把缓存读写重定向到 tmp_path，避免污染真实 settings.json。"""
    cache_file = tmp_path / "settings.json"

    def fake_load():
        if not cache_file.exists():
            return {}
        return json.loads(cache_file.read_text(encoding="utf-8"))

    def fake_save(config):
        cache_file.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    monkeypatch.setattr("ksadk.identity.resolver._load_identity_cache", lambda: fake_load().__getitem__("cloud").get("IDENTITY_CACHE", {}) if fake_load() else {})
    # 直接 patch _load/_save 更简单
    _cache = {}

    def load():
        return dict(_cache)

    def save(cache):
        _cache.clear()
        _cache.update(cache)

    monkeypatch.setattr("ksadk.identity.resolver._load_identity_cache", load)
    monkeypatch.setattr("ksadk.identity.resolver._save_identity_cache", save)
    return _cache


def test_get_cached_identity_miss(isolated_cache):
    assert get_cached_identity("AKLTnone") is None
    assert get_cached_user_uuid("AKLTnone") is None


def test_invalidate_cache(isolated_cache):
    # 写入一个条目
    fp = _ak_fingerprint("AKLTtest")
    isolated_cache[fp] = {"ak_fingerprint": fp, "user_uuid": "uuid-x"}
    # 清空
    invalidate_cache("AKLTtest")
    assert get_cached_user_uuid("AKLTtest") is None
    # 全清
    isolated_cache[fp] = {"ak_fingerprint": fp, "user_uuid": "uuid-x"}
    invalidate_cache(None)
    assert get_cached_user_uuid("AKLTtest") is None


# ---------------------------------------------------------------------------
# resolve_identity 集成测试（mock IAM SDK）
# ---------------------------------------------------------------------------


def _mock_sdk_parts():
    """构造 mock 的 sdk_parts 元组。"""
    return tuple(MagicMock() for _ in range(6))


def test_resolve_identity_cache_hit_no_iam_call(isolated_cache, monkeypatch):
    """缓存命中时不调 IAM。"""
    fp = _ak_fingerprint("AKLTtest")
    isolated_cache[fp] = {
        "ak_fingerprint": fp,
        "user_uuid": "uuid-cached",
        "main_account_id": "2000003485",
        "user_name": "cached-user",
        "krn": "krn:ksc:iam::2000003485:user/cached-user",
    }
    called = MagicMock()
    monkeypatch.setattr("ksadk.identity.resolver._import_iam_sdk", lambda: called())
    r = resolve_identity(access_key="AKLTtest", secret_key="SKtest")
    assert called.call_count == 0  # 缓存命中，未调 IAM
    assert r is not None
    assert r.user_uuid == "uuid-cached"
    assert r.main_account_id == "2000003485"


def test_resolve_identity_cache_miss_invokes_iam(isolated_cache, monkeypatch):
    """缓存 miss 时调 IAM 两步链路并写缓存。"""
    sdk_parts = _mock_sdk_parts()
    IamClient = sdk_parts[0]
    ListReq = sdk_parts[1]
    GetReq = sdk_parts[2]

    # mock client 实例
    client = MagicMock()
    IamClient.return_value = client
    client.ListAllUserAccessKeys.return_value = json.dumps(
        {"AccessKeyList": [{"AccessKey": "AKLTtest", "UserName": "xiayu"}]}
    )
    client.GetUser.return_value = json.dumps(
        {"GetUserResult": {"User": {"UserId": "uuid-new", "Krn": "krn:ksc:iam::2000003485:user/xiayu"}}}
    )

    monkeypatch.setattr("ksadk.identity.resolver._import_iam_sdk", lambda: sdk_parts)

    r = resolve_identity(access_key="AKLTtest", secret_key="SKtest")
    assert r is not None
    assert r.user_uuid == "uuid-new"
    assert r.main_account_id == "2000003485"
    assert r.user_name == "xiayu"
    # 验证写缓存
    fp = _ak_fingerprint("AKLTtest")
    assert fp in isolated_cache
    assert isolated_cache[fp]["user_uuid"] == "uuid-new"


def test_resolve_identity_ak_not_in_list_returns_none(isolated_cache, monkeypatch):
    """AK 不在子用户列表（主账号 AK）返回 None。"""
    sdk_parts = _mock_sdk_parts()
    IamClient = sdk_parts[0]
    client = MagicMock()
    IamClient.return_value = client
    client.ListAllUserAccessKeys.return_value = json.dumps(
        {"AccessKeyList": [{"AccessKey": "OTHER_AK", "UserName": "other"}]}
    )
    monkeypatch.setattr("ksadk.identity.resolver._import_iam_sdk", lambda: sdk_parts)

    r = resolve_identity(access_key="AKLTtest", secret_key="SKtest")
    assert r is None
    client.GetUser.assert_not_called()  # 没找到 AK 就不调 GetUser


def test_resolve_identity_network_failure_returns_none(isolated_cache, monkeypatch):
    """IAM 调用异常返回 None 不抛。"""
    sdk_parts = _mock_sdk_parts()
    IamClient = sdk_parts[0]
    IamClient.side_effect = Exception("network error")
    monkeypatch.setattr("ksadk.identity.resolver._import_iam_sdk", lambda: sdk_parts)

    r = resolve_identity(access_key="AKLTtest", secret_key="SKtest")
    assert r is None


def test_resolve_identity_intranet_fallback(isolated_cache, monkeypatch):
    """公网失败（InnerAccountCanOnlyAccessThroughIntranet）时 fallback 内网。"""
    sdk_parts = _mock_sdk_parts()
    IamClient = sdk_parts[0]
    client = MagicMock()
    IamClient.return_value = client
    # 第一次（公网）抛内网错误，第二次（内网）成功
    client.ListAllUserAccessKeys.side_effect = [
        Exception('{"Error":{"Code":"InnerAccountCanOnlyAccessThroughIntranet"}}'),
        json.dumps({"AccessKeyList": [{"AccessKey": "AKLTtest", "UserName": "inner-user"}]}),
    ]
    client.GetUser.return_value = json.dumps(
        {"GetUserResult": {"User": {"UserId": "uuid-inner", "Krn": "krn:ksc:iam::2000003485:user/inner-user"}}}
    )
    intranet_host = "iam." + "inner." + "api.ksyun.com"
    monkeypatch.setenv("KSYUN_IAM_INTRANET_URL", f"http://{intranet_host}")
    monkeypatch.setattr("ksadk.identity.resolver._import_iam_sdk", lambda: sdk_parts)

    r = resolve_identity(access_key="AKLTtest", secret_key="SKtest")
    assert r is not None
    assert r.user_uuid == "uuid-inner"
    # 验证调了两次（公网 + 内网）
    assert client.ListAllUserAccessKeys.call_count == 2


def test_resolve_identity_no_credentials_returns_none(isolated_cache):
    assert resolve_identity(access_key="", secret_key="SK") is None
    assert resolve_identity(access_key="AK", secret_key="") is None


def test_resolve_identity_force_refresh_bypasses_cache(isolated_cache, monkeypatch):
    """force_refresh=True 绕过缓存重新反查。"""
    fp = _ak_fingerprint("AKLTtest")
    isolated_cache[fp] = {"ak_fingerprint": fp, "user_uuid": "old-uuid"}
    sdk_parts = _mock_sdk_parts()
    IamClient = sdk_parts[0]
    client = MagicMock()
    IamClient.return_value = client
    client.ListAllUserAccessKeys.return_value = json.dumps(
        {"AccessKeyList": [{"AccessKey": "AKLTtest", "UserName": "u"}]}
    )
    client.GetUser.return_value = json.dumps(
        {"GetUserResult": {"User": {"UserId": "new-uuid", "Krn": "krn:ksc:iam::2000003485:user/u"}}}
    )
    monkeypatch.setattr("ksadk.identity.resolver._import_iam_sdk", lambda: sdk_parts)

    r = resolve_identity(access_key="AKLTtest", secret_key="SK", force_refresh=True)
    assert r is not None
    assert r.user_uuid == "new-uuid"  # 用新值，不是缓存的 old-uuid


def test_get_cached_identity_returns_full_identity(isolated_cache):
    fp = _ak_fingerprint("AKLTtest")
    isolated_cache[fp] = {
        "ak_fingerprint": fp,
        "user_uuid": "uuid-x",
        "main_account_id": "2000003485",
        "user_name": "u",
        "krn": "krn:ksc:iam::2000003485:user/u",
    }
    r = get_cached_identity("AKLTtest")
    assert r is not None
    assert r.user_uuid == "uuid-x"
    assert r.main_account_id == "2000003485"
