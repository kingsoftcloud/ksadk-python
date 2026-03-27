# -*- coding: utf-8 -*-
"""
时间参数统一处理：与 API 时间戳互转 UTC ISO 8601 字符串，便于 LLM 理解。
spec：涉及 ctime/mtime、开始/结束时间等，统一使用 UTC ISO 8601；若 API 为时间戳则由脚本兼容入参与出参。
"""
from datetime import datetime, timezone
from typing import Any, Optional

# 常见 API 时间字段名，出参时从时间戳转为 ISO 8601
TIME_FIELD_NAMES = frozenset(
    (
        "ctime",
        "mtime",
        "create_time",
        "update_time",
        "start_time",
        "end_time",
        "start_date_time",
        "end_date_time",
        "regtime",
    )
)


def timestamp_to_iso_utc(ts: Optional[Any]) -> Optional[str]:
    """
    将 API 返回的时间戳转为 UTC ISO 8601 字符串。
    支持秒级（10 位）或毫秒级（13 位）整数。
    """
    if ts is None:
        return None
    try:
        t = int(ts)
    except (TypeError, ValueError):
        return None
    if t <= 0:
        return None
    # 毫秒级约 13 位
    if t > 1e12:
        t = t / 1000.0
    dt = datetime.utcfromtimestamp(t).replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_to_calendar_time(iso: Optional[str]) -> Optional[dict]:
    """
    将 ISO 8601 或日期字符串转为日历 API 的 v7_calendar_time 结构。
    仅日期（yyyy-mm-dd）返回 {"date": "yyyy-mm-dd"}；含时间则返回 {"datetime": "..."}（原样下发给服务端）。
    东8区建议：时间请带 Z（UTC）或 +08:00，避免无后缀时服务端按本地/UTC 解释不一致导致 8 小时偏差。
    """
    if not iso or not isinstance(iso, str):
        return None
    s = iso.strip()
    if not s:
        return None
    # 仅日期：yyyy-mm-dd
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        try:
            datetime.strptime(s, "%Y-%m-%d")
            return {"date": s}
        except ValueError:
            pass
    # 含时间：保留原样或规范为 Z
    if s.endswith("Z") or "+" in s or s.count("-") > 2:
        return {"datetime": s}
    return {"datetime": s}


def iso_to_timestamp(iso: Optional[str]) -> Optional[int]:
    """
    将 ISO 8601 字符串转为 API 所需的时间戳（秒级）。
    支持带时区：Z、+00:00、+08:00 等。无时区后缀的字符串（naive）按 UTC 处理。
    东8区注意：若传入本地时间请带 +08:00（如 2026-03-04T14:00:00+08:00），
    否则 "2026-03-04T14:00:00" 会被当作 14:00 UTC，对应北京时间 22:00，会议会晚 8 小时。
    """
    if not iso or not isinstance(iso, str):
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return None


def response_timestamps_to_iso(data: Any) -> Any:
    """
    递归将 dict/list 中已知时间字段（如 ctime, mtime）从时间戳转为 UTC ISO 8601。
    直接修改并返回 data（兼容嵌套的 depts 等）。
    """
    if data is None:
        return data
    if isinstance(data, dict):
        for k, v in list(data.items()):
            if k in TIME_FIELD_NAMES and v is not None:
                try:
                    data[k] = timestamp_to_iso_utc(v)
                except Exception:
                    pass
            else:
                data[k] = response_timestamps_to_iso(v)
        return data
    if isinstance(data, list):
        for i, item in enumerate(data):
            data[i] = response_timestamps_to_iso(item)
        return data
    return data
