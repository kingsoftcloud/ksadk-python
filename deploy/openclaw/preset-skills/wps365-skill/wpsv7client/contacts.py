# -*- coding: utf-8 -*-
"""
通讯录 V7 API 封装。
参考 apis-dev/spec/internal/iam：GET /v7/users/search 企业用户搜索。
按人名（keyword）搜索，支持同名返回多条；出参时间字段统一转为 UTC ISO 8601。
"""
from typing import Any, Optional

from .base import WpsV7Client
from .time_util import response_timestamps_to_iso


def _normalize_resp(resp: Any) -> dict:
    """保持 {code, msg, data} 结构，仅对 data 做时间字段转换。"""
    if resp is None:
        return {}
    if not isinstance(resp, dict):
        return resp if isinstance(resp, dict) else {}
    out = dict(resp)
    if "data" in out and out["data"] is not None:
        out["data"] = response_timestamps_to_iso(out["data"])
    return out


def search_users(
    keyword: str,
    client: Optional[WpsV7Client] = None,
    page_size: int = 50,
    page_token: Optional[str] = None,
    with_total: bool = False,
    status: Optional[str] = None,
) -> dict:
    """
    按关键字搜索企业用户（通讯录）。
    GET /v7/users/search。
    keyword: 搜索关键字，支持人名等；同名时返回多条，由调用方按需展示。
    status: 用户状态，如 active；公网建议传 active。
    """
    if not keyword or not str(keyword).strip():
        return _normalize_resp({"code": 0, "msg": "", "data": {"items": [], "next_page_token": ""}})
    c = client or WpsV7Client()
    params = {
        "keyword": str(keyword).strip(),
        "page_size": min(1000, max(1, page_size)),
    }
    if page_token:
        params["page_token"] = page_token
    if with_total:
        params["with_total"] = True
    if status:
        params["status"] = status
    else:
        params["status"] = "active"
    resp = c.get("/v7/users/search", params=params)
    return _normalize_resp(resp)
