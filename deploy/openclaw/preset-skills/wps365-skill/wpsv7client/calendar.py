# -*- coding: utf-8 -*-
"""
日历与日程 V7 API 封装。
参考 apis-dev/spec/internal/calendar：日历与日程增删改查。
时间入参支持 UTC ISO 8601 或日期；出参时间字段统一转为 UTC ISO 8601。
"""
from typing import Any, Optional

from .base import WpsV7Client
from .time_util import iso_to_calendar_time, response_timestamps_to_iso


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


# ---------- 日历 ----------


def list_calendars(
    client: Optional[WpsV7Client] = None,
    with_total: bool = False,
    page_token: Optional[str] = None,
    page_size: int = 20,
) -> dict:
    """日历列表。GET /v7/calendars"""
    c = client or WpsV7Client()
    params = {"with_total": with_total, "page_size": min(20, max(1, page_size))}
    if page_token:
        params["page_token"] = page_token
    resp = c.get("/v7/calendars", params=params)
    return _normalize_resp(resp)


def get_calendar(calendar_id: str, client: Optional[WpsV7Client] = None) -> dict:
    """查看日历。GET /v7/calendars/{calendar_id}"""
    c = client or WpsV7Client()
    resp = c.get("/v7/calendars/" + calendar_id)
    return _normalize_resp(resp)


def create_calendar(
    summary: str,
    color: str,
    description: str = "",
    client: Optional[WpsV7Client] = None,
) -> dict:
    """创建日历。POST /v7/calendars/create。color 示例: #FF0000FF"""
    c = client or WpsV7Client()
    body = {"summary": summary, "color": color, "description": description or ""}
    resp = c.post("/v7/calendars/create", json=body)
    return _normalize_resp(resp)


def update_calendar(
    calendar_id: str,
    summary: Optional[str] = None,
    color: Optional[str] = None,
    description: Optional[str] = None,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """修改日历。POST /v7/calendars/{calendar_id}/update"""
    c = client or WpsV7Client()
    body = {}
    if summary is not None:
        body["summary"] = summary
    if color is not None:
        body["color"] = color
    if description is not None:
        body["description"] = description
    resp = c.post(f"/v7/calendars/{calendar_id}/update", json=body)
    return _normalize_resp(resp)


def delete_calendar(calendar_id: str, client: Optional[WpsV7Client] = None) -> dict:
    """删除日历。POST /v7/calendars/{calendar_id}/delete"""
    c = client or WpsV7Client()
    resp = c.post(f"/v7/calendars/{calendar_id}/delete")
    return _normalize_resp(resp)


# ---------- 日程 ----------


def list_events(
    calendar_id: str,
    start_time: str,
    end_time: str,
    client: Optional[WpsV7Client] = None,
    page_token: Optional[str] = None,
    page_size: int = 30,
) -> dict:
    """
    查询单个日历的日程列表。GET /v7/calendars/{id}/events。
    start_time/end_time 为 UTC ISO 8601 或 RFC3339，区间不超过 31 天。
    """
    c = client or WpsV7Client()
    params = {
        "start_time": start_time,
        "end_time": end_time,
        "page_size": min(100, max(1, page_size)),
    }
    if page_token:
        params["page_token"] = page_token
    resp = c.get(f"/v7/calendars/{calendar_id}/events", params=params)
    return _normalize_resp(resp)


def get_event(
    calendar_id: str,
    event_id: str,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """查询日程。GET /v7/calendars/{calendar_id}/events/{event_id}"""
    c = client or WpsV7Client()
    resp = c.get(f"/v7/calendars/{calendar_id}/events/{event_id}")
    return _normalize_resp(resp)


def list_free_busy(
    start_time: str,
    end_time: str,
    user_ids: Optional[list] = None,
    room_ids: Optional[list] = None,
    calendar_type: str = "primary",
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    查询用户或会议室的日程忙闲。GET /v7/free_busy_list。
    查询时间区间需满足 end_time - start_time <= 7 天。
    user_ids 与 room_ids 二选一，至少传一个；user_ids/room_ids 为 id 列表，最多 50 个。
    返回各对象在区间内的忙时间段（busy_times），其余即为空闲。
    """
    c = client or WpsV7Client()
    if not user_ids and not room_ids:
        return _normalize_resp({"code": 400, "msg": "user_ids 与 room_ids 至少传一个", "data": None})
    params = {"start_time": start_time, "end_time": end_time, "calendar_type": calendar_type}
    if user_ids:
        params["user_ids"] = user_ids[:50] if isinstance(user_ids, list) else [str(user_ids)]
    if room_ids:
        params["room_ids"] = room_ids[:50] if isinstance(room_ids, list) else [str(room_ids)]
    resp = c.get("/v7/free_busy_list", params=params)
    return _normalize_resp(resp)


def _build_locations(location: Optional[str] = None, locations: Optional[list] = None) -> list:
    """构建 locations 数组，最多 1 个。v7_calendar_location: { name: string }"""
    if locations is not None and isinstance(locations, list) and len(locations) > 0:
        return [{"name": str(loc.get("name", loc))} if isinstance(loc, dict) else {"name": str(loc)} for loc in locations[:1]]
    if location:
        return [{"name": str(location)}]
    return []


def _build_attachments(attachment_file_ids: Optional[list] = None) -> list:
    """构建 attachments 数组，最多 20 个。v7_calendar_attachment_file: { file_id: string }"""
    if not attachment_file_ids:
        return []
    out = []
    for x in attachment_file_ids[:20]:
        if isinstance(x, dict) and x.get("file_id"):
            out.append({"file_id": str(x["file_id"])})
        elif isinstance(x, str) and x:
            out.append({"file_id": x})
    return out


def batch_create_event_attendees(
    calendar_id: str,
    event_id: str,
    user_ids: list,
    client: Optional[WpsV7Client] = None,
    is_notification: Optional[bool] = None,
) -> dict:
    """
    添加日程参与者。POST /v7/calendars/{id}/events/{event_id}/attendees/batch_create。
    user_ids: 用户 id 列表（通讯录获取），每次最多 200，日程参与者总数最多 1000。
    """
    c = client or WpsV7Client()
    attendees = [{"type": "user", "user_id": str(uid)} for uid in user_ids[:200] if uid]
    if not attendees:
        return _normalize_resp({"code": 0, "msg": "", "data": {"items": []}})
    body = {"attendees": attendees}
    if is_notification is not None:
        body["is_notification"] = is_notification
    resp = c.post(f"/v7/calendars/{calendar_id}/events/{event_id}/attendees/batch_create", json=body)
    return _normalize_resp(resp)


def batch_delete_event_attendees(
    calendar_id: str,
    event_id: str,
    attendee_ids: list,
    client: Optional[WpsV7Client] = None,
    is_notification: Optional[bool] = None,
) -> dict:
    """
    删除日程参与者。POST /v7/calendars/{id}/events/{event_id}/attendees/batch_delete。
    attendee_ids: 参与者 id 列表（从参与者列表接口获取），每次最多 50。
    """
    c = client or WpsV7Client()
    ids = [str(aid) for aid in attendee_ids[:50] if aid]
    if not ids:
        return _normalize_resp({"code": 0, "msg": "", "data": None})
    body = {"attendee_ids": ids}
    if is_notification is not None:
        body["is_notification"] = is_notification
    resp = c.post(f"/v7/calendars/{calendar_id}/events/{event_id}/attendees/batch_delete", json=body)
    return _normalize_resp(resp)


def create_event(
    calendar_id: str,
    start_time: str,
    end_time: str,
    summary: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    locations: Optional[list] = None,
    attachment_file_ids: Optional[list] = None,
    attendee_user_ids: Optional[list] = None,
    client: Optional[WpsV7Client] = None,
    **kwargs: Any,
) -> dict:
    """
    创建日程。POST /v7/calendars/{id}/events/create。
    start_time/end_time 支持 ISO 8601 或日期 yyyy-mm-dd；东8区建议带 Z 或 +08:00 避免 8 小时偏差。
    location/locations: 地点，最多 1 个（传名称字符串或 [{"name": "..."}]）。
    attachment_file_ids: 附件 file_id 列表，最多 20 个（v7_file.id）。
    attendee_user_ids: 参与者用户 id 列表，创建成功后会调用添加参与者接口。
    """
    c = client or WpsV7Client()
    st = iso_to_calendar_time(start_time)
    et = iso_to_calendar_time(end_time)
    if not st or not et:
        raise ValueError("start_time 与 end_time 需为有效 ISO 8601 或 yyyy-mm-dd")
    body = {"start_time": st, "end_time": et, **kwargs}
    if summary is not None:
        body["summary"] = summary
    if description is not None:
        body["description"] = description
    locs = _build_locations(location=location, locations=locations)
    if locs:
        body["locations"] = locs
    atts = _build_attachments(attachment_file_ids)
    if atts:
        body["attachments"] = atts
    resp = c.post(f"/v7/calendars/{calendar_id}/events/create", json=body)
    out = _normalize_resp(resp)
    if out.get("code") == 0 and attendee_user_ids and out.get("data") and out["data"].get("id"):
        batch_create_event_attendees(calendar_id, out["data"]["id"], attendee_user_ids, client=c)
    return out


def update_event(
    calendar_id: str,
    event_id: str,
    client: Optional[WpsV7Client] = None,
    summary: Optional[str] = None,
    description: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    location: Optional[str] = None,
    locations: Optional[list] = None,
    attachment_file_ids: Optional[list] = None,
    **kwargs: Any,
) -> dict:
    """
    修改日程。POST /v7/calendars/{id}/events/{event_id}/update。
    时间参数为 ISO 8601。location/locations 最多 1 个；attachment_file_ids 最多 20 个。
    """
    c = client or WpsV7Client()
    body = dict(kwargs)
    if summary is not None:
        body["summary"] = summary
    if description is not None:
        body["description"] = description
    if start_time is not None:
        st = iso_to_calendar_time(start_time)
        if st:
            body["start_time"] = st
    if end_time is not None:
        et = iso_to_calendar_time(end_time)
        if et:
            body["end_time"] = et
    locs = _build_locations(location=location, locations=locations)
    if locs:
        body["locations"] = locs
    if attachment_file_ids is not None:
        body["attachments"] = _build_attachments(attachment_file_ids)
    resp = c.post(f"/v7/calendars/{calendar_id}/events/{event_id}/update", json=body)
    return _normalize_resp(resp)


def delete_event(
    calendar_id: str,
    event_id: str,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """删除日程。POST /v7/calendars/{calendar_id}/events/{event_id}/delete"""
    c = client or WpsV7Client()
    resp = c.post(f"/v7/calendars/{calendar_id}/events/{event_id}/delete")
    return _normalize_resp(resp)
