"""从 AK/SK 反查金山云子账号身份（user uuid + 主账号 ID）。

控制面 ctx.sub_account_id 只从请求头 X-Ksc-User-uuid 取，KOP 不会根据 AK/SK 自动注入。
本模块用 AK/SK 调 IAM 的 ListAllUserAccessKeys + GetUser 两步反查：
  1. ListAllUserAccessKeys（无参）返回所有子用户 AK + UserName
  2. GetUser(UserName) 返回 User.UserId（子账号 uuid）+ User.Krn（含主账号 ID）

反查结果缓存到 ~/.agentengine/settings.json 的 cloud.IDENTITY_CACHE（按 AK 指纹索引，
多账号安全）。任何失败返回 None，不抛异常，调用方降级为不注入 header（退化为当前行为）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# krn:ksc:iam::<主账号ID>:user/<name>
_KRN_ACCOUNT_RE = re.compile(r"krn:ksc:iam::([^:]+):user/")


@dataclass(frozen=True)
class ResolvedIdentity:
    """AK/SK 反查到的身份信息。"""

    user_uuid: Optional[str]        # 子账号 UserId（X-Ksc-User-uuid 值）；主账号 AK 时 None
    main_account_id: Optional[str]  # 从 Krn 提取的主账号 ID
    user_name: Optional[str]        # 子账号 UserName（调试用）
    krn: Optional[str]              # 原始 Krn（调试用）
    ak_fingerprint: str             # sha256(AK)[:16]，缓存 key


# ---------------------------------------------------------------------------
# AK 指纹 + Krn 解析
# ---------------------------------------------------------------------------


def _ak_fingerprint(access_key: str) -> str:
    """sha256(AK)[:16]，防碰撞且不暴露明文 AK。"""
    return hashlib.sha256(access_key.encode("utf-8")).hexdigest()[:16]


def _extract_main_account_id_from_krn(krn: Optional[str]) -> Optional[str]:
    """从 krn:ksc:iam::<主账号ID>:user/<name> 提取主账号 ID。"""
    if not krn:
        return None
    match = _KRN_ACCOUNT_RE.search(krn)
    if not match:
        return None
    account_id = match.group(1).strip()
    return account_id or None


# ---------------------------------------------------------------------------
# IAM endpoint 解析
# ---------------------------------------------------------------------------


def _resolve_iam_endpoint() -> tuple[str, str]:
    """解析 IAM endpoint，返回 (host, scheme)。优先级：KSYUN_IAM_URL > IAM_URL > 默认。"""
    raw = (os.getenv("KSYUN_IAM_URL") or os.getenv("IAM_URL") or "").strip()
    if not raw:
        return "iam.api.ksyun.com", "https"
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = (parsed.netloc or parsed.path or "iam.api.ksyun.com").strip()
    scheme = (parsed.scheme or "https").strip().lower() or "https"
    return host, scheme


def _resolve_iam_intranet_endpoint() -> tuple[str, str] | None:
    """解析显式配置的 IAM 内网 endpoint。未配置时不启用内网 fallback。"""
    raw = (os.getenv("KSYUN_IAM_INTRANET_URL") or os.getenv("IAM_INTRANET_URL") or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    host = (parsed.netloc or parsed.path or "").strip()
    if not host:
        return None
    scheme = (parsed.scheme or "http").strip().lower() or "http"
    return host, scheme


def _should_retry_intranet(error: Exception | None) -> bool:
    """判断是否应回退到内网 endpoint（内部账号只能内网访问时）。"""
    if error is None:
        return False
    return "InnerAccountCanOnlyAccessThroughIntranet" in str(error)


# ---------------------------------------------------------------------------
# ksyun SDK 惰性导入
# ---------------------------------------------------------------------------


def _import_iam_sdk():
    """惰性导入 ksyun IAM SDK，失败返回 None。"""
    try:
        from ksyun.client.iam.v20151101.client import IamClient
        from ksyun.client.iam.v20151101.models import (
            GetUserRequest,
            ListAllUserAccessKeysRequest,
        )
        from ksyun.common.credential import Credential
        from ksyun.common.profile.client_profile import ClientProfile
        from ksyun.common.profile.http_profile import HttpProfile
    except Exception as exc:
        logger.warning("导入 ksyun IAM SDK 失败: %s", exc)
        return None
    return (
        IamClient,
        ListAllUserAccessKeysRequest,
        GetUserRequest,
        Credential,
        ClientProfile,
        HttpProfile,
    )


def _build_iam_client(*, access_key: str, secret_key: str, host: str, scheme: str, sdk_parts):
    """构造 IamClient（复用服务端 iam/client.py 模式）。"""
    IamClient, _, _, Credential, ClientProfile, HttpProfile = sdk_parts
    http_profile = HttpProfile()
    http_profile.endpoint = host
    http_profile.reqMethod = "POST"
    http_profile.reqTimeout = 30
    http_profile.scheme = scheme
    client_profile = ClientProfile()
    client_profile.httpProfile = http_profile
    cred = Credential(access_key, secret_key)
    return IamClient(cred, "cn-beijing-6", profile=client_profile)


# ---------------------------------------------------------------------------
# IAM 调用
# ---------------------------------------------------------------------------


def _call_list_all_user_access_keys(client, sdk_parts) -> list[dict]:
    """调 ListAllUserAccessKeys，返回 AccessKey 列表（每项含 AccessKey + UserName）。"""
    _, ListAllUserAccessKeysRequest, _, _, _, _ = sdk_parts
    resp = client.ListAllUserAccessKeys(ListAllUserAccessKeysRequest())
    if isinstance(resp, str):
        resp = json.loads(resp)
    if not isinstance(resp, dict):
        return []
    keys = resp.get("AccessKeyList") or resp.get("AccessKeys") or []
    return [k for k in keys if isinstance(k, dict)]


def _call_get_user(client, user_name: str, sdk_parts) -> dict:
    """调 GetUser(UserName)，返回 User dict。"""
    _, _, GetUserRequest, _, _, _ = sdk_parts
    req = GetUserRequest()
    req.UserName = user_name
    resp = client.GetUser(req)
    if isinstance(resp, str):
        resp = json.loads(resp)
    if not isinstance(resp, dict):
        return {}
    return resp.get("GetUserResult", {}).get("User", {}) or {}


def _find_username_by_ak(access_keys: list[dict], target_ak: str) -> Optional[str]:
    """在 AccessKey 列表里找目标 AK 对应的 UserName。"""
    for item in access_keys:
        ak = str(item.get("AccessKey") or item.get("AccessKeyId") or "").strip()
        if ak and ak == target_ak:
            return str(item.get("UserName") or "").strip() or None
    return None


# ---------------------------------------------------------------------------
# 缓存读写（settings.json 的 cloud.IDENTITY_CACHE）
# ---------------------------------------------------------------------------


def _load_identity_cache() -> dict:
    """读 settings.json 的 cloud.IDENTITY_CACHE，失败返回空 dict。"""
    try:
        from ksadk.configs.global_config import load_global_config

        config = load_global_config()
    except Exception as exc:
        logger.warning("读取 global_config 失败: %s", exc)
        return {}
    cloud = config.get("cloud") or {}
    cache = cloud.get("IDENTITY_CACHE") if isinstance(cloud, dict) else None
    return cache if isinstance(cache, dict) else {}


def _save_identity_cache(cache: dict) -> None:
    """写 settings.json 的 cloud.IDENTITY_CACHE（merge，保留其他字段）。"""
    try:
        from ksadk.configs.global_config import load_global_config, save_global_config

        config = load_global_config()
        cloud = dict(config.get("cloud") or {})
        cloud["IDENTITY_CACHE"] = cache
        config["cloud"] = cloud
        save_global_config(config)
    except Exception as exc:
        logger.warning("写入 identity 缓存失败: %s", exc)


def _read_cache_entry(access_key: str) -> Optional[dict]:
    """读指定 AK 的缓存条目，指纹不匹配返回 None。"""
    if not access_key:
        return None
    fingerprint = _ak_fingerprint(access_key)
    cache = _load_identity_cache()
    entry = cache.get(fingerprint)
    if not isinstance(entry, dict):
        return None
    if entry.get("ak_fingerprint") != fingerprint:
        return None
    return entry


def _write_cache_entry(access_key: str, entry: dict) -> None:
    """写指定 AK 的缓存条目（merge，不破坏其他条目）。"""
    if not access_key:
        return
    fingerprint = _ak_fingerprint(access_key)
    cache = _load_identity_cache()
    cache[fingerprint] = entry
    _save_identity_cache(cache)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def resolve_identity(
    *,
    access_key: str,
    secret_key: str,
    force_refresh: bool = False,
) -> Optional[ResolvedIdentity]:
    """用 AK/SK 反查子账号身份。

    先读缓存命中即返回；未命中调 IAM 两步链路并写缓存。
    任何失败返回 None（不抛异常，调用方降级）。
    """
    if not access_key or not secret_key:
        return None

    fingerprint = _ak_fingerprint(access_key)

    # 1. 读缓存
    if not force_refresh:
        entry = _read_cache_entry(access_key)
        if entry:
            return ResolvedIdentity(
                user_uuid=entry.get("user_uuid"),
                main_account_id=entry.get("main_account_id"),
                user_name=entry.get("user_name"),
                krn=entry.get("krn"),
                ak_fingerprint=fingerprint,
            )

    # 2. 调 IAM 反查。内网 fallback 只在显式配置 IAM_INTRANET_URL 时启用，
    # 避免公开包硬编码内部 service endpoint。
    sdk_parts = _import_iam_sdk()
    if sdk_parts is None:
        return None

    host, scheme = _resolve_iam_endpoint()
    candidates = [(host, scheme)]
    intranet_endpoint = _resolve_iam_intranet_endpoint()
    if intranet_endpoint and intranet_endpoint not in candidates:
        candidates.append(intranet_endpoint)

    user_name: Optional[str] = None
    user: dict = {}
    last_exc: Optional[Exception] = None
    for cand_host, cand_scheme in candidates:
        try:
            client = _build_iam_client(
                access_key=access_key,
                secret_key=secret_key,
                host=cand_host,
                scheme=cand_scheme,
                sdk_parts=sdk_parts,
            )
            access_keys = _call_list_all_user_access_keys(client, sdk_parts)
            user_name = _find_username_by_ak(access_keys, access_key)
            if not user_name:
                # AK 不在子用户列表（可能是主账号 AK），无法反查 user uuid
                logger.warning(
                    "AK 指纹 %s 未在 ListAllUserAccessKeys 找到匹配（可能是主账号 AK）",
                    fingerprint,
                )
                return None
            user = _call_get_user(client, user_name, sdk_parts)
            host, scheme = cand_host, cand_scheme  # 记录成功的 endpoint 用于缓存
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            # 仅在"内部账号需内网访问"时才 fallback 到内网 endpoint
            if not _should_retry_intranet(exc):
                break

    if last_exc is not None:
        logger.warning("反查子账号身份失败 (AK 指纹 %s): %s", fingerprint, last_exc)
        return None

    user_uuid = str(user.get("UserId") or "").strip() or None
    krn = str(user.get("Krn") or "").strip() or None
    main_account_id = _extract_main_account_id_from_krn(krn)

    identity = ResolvedIdentity(
        user_uuid=user_uuid,
        main_account_id=main_account_id,
        user_name=user_name,
        krn=krn,
        ak_fingerprint=fingerprint,
    )

    # 3. 写缓存
    _write_cache_entry(
        access_key,
        {
            "ak_fingerprint": fingerprint,
            "user_uuid": user_uuid,
            "main_account_id": main_account_id,
            "user_name": user_name,
            "krn": krn,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "iam_endpoint": host,
            "iam_scheme": scheme,
        },
    )

    return identity


def get_cached_user_uuid(access_key: str) -> Optional[str]:
    """只读缓存拿 user uuid，不触发反查（dry-run 用）。"""
    entry = _read_cache_entry(access_key)
    if not entry:
        return None
    uuid = entry.get("user_uuid")
    return str(uuid).strip() or None if uuid else None


def get_cached_identity(access_key: str) -> Optional[ResolvedIdentity]:
    """只读缓存拿完整身份（含 main_account_id），不触发反查（dry-run 用）。"""
    if not access_key:
        return None
    entry = _read_cache_entry(access_key)
    if not entry:
        return None
    return ResolvedIdentity(
        user_uuid=entry.get("user_uuid"),
        main_account_id=entry.get("main_account_id"),
        user_name=entry.get("user_name"),
        krn=entry.get("krn"),
        ak_fingerprint=_ak_fingerprint(access_key),
    )


def invalidate_cache(access_key: Optional[str] = None) -> None:
    """删除指定 AK 的缓存条目；access_key=None 清空所有。"""
    cache = _load_identity_cache()
    if access_key is None:
        cache = {}
    else:
        fingerprint = _ak_fingerprint(access_key)
        cache.pop(fingerprint, None)
    _save_identity_cache(cache)
