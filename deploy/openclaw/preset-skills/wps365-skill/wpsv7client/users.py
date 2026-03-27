# -*- coding: utf-8 -*-
"""
用户域 V7 API 封装。
参考 apis-dev/spec/internal/iam：GET /v7/users/current 等。
时间字段（ctime/mtime 等）在出参中统一转为 UTC ISO 8601 字符串。
"""
from typing import Optional

from .base import WpsV7Client
from .time_util import response_timestamps_to_iso


def get_user_by_id(user_id: str, client: Optional[WpsV7Client] = None) -> dict:
    """
    根据用户ID查询用户信息。
    对应 V7 接口: GET /v7/users/{user_id}。
    返回完整响应 {code, msg, data}，其中 data 内时间字段已转为 UTC ISO 8601。
    调用方需先判断 code 是否为 0，再使用 data。
    """
    c = client or WpsV7Client()
    resp = c.get(f"/v7/users/{user_id}")
    if not isinstance(resp, dict):
        return resp
    if "data" in resp and resp["data"] is not None:
        resp = dict(resp)
        resp["data"] = response_timestamps_to_iso(resp["data"]) or {}
    return resp


def get_current_user(client: Optional[WpsV7Client] = None) -> dict:
    """
    获取当前登录用户信息。
    对应 V7 接口: GET /v7/users/current。
    返回完整响应 {code, msg, data}，其中 data 内时间字段已转为 UTC ISO 8601。
    调用方需先判断 code 是否为 0，再使用 data。
    """
    c = client or WpsV7Client()
    resp = c.get("/v7/users/current")
    if not isinstance(resp, dict):
        return resp
    if "data" in resp and resp["data"] is not None:
        resp = dict(resp)
        resp["data"] = response_timestamps_to_iso(resp["data"]) or {}
    return resp
