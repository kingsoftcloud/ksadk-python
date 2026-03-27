# -*- coding: utf-8 -*-
"""
会议域 V7 API 封装。
参考 apis-dev/spec/internal/meeting：会议创建/查询/列表、参会人管理。

约定：
- 入参时间统一使用 UTC ISO 8601 字符串（如 2026-03-02T08:00:00Z）
- 若 API 使用时间戳（秒/毫秒），则在客户端内做入参与出参的自动兼容
"""

from typing import Any, Iterable, Optional

from .base import WpsV7Client
from .time_util import iso_to_timestamp, response_timestamps_to_iso


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


def _build_participants(participant_ids: Optional[Iterable[str]], role: str = "attendee") -> Optional[list]:
    if not participant_ids:
        return None
    items = []
    for pid in participant_ids:
        if not pid:
            continue
        items.append({"role": role, "id": str(pid)})
    return items or None


def create_meeting(
    subject: str,
    start_time: str,
    end_time: str,
    participant_ids: Optional[Iterable[str]] = None,
    *,
    approval_required_to_join: Optional[bool] = None,
    allow_attendee_to_join: Optional[str] = None,
    attendee_mic_status: Optional[str] = None,
    attendee_camera_status: Optional[str] = None,
    generate_summary: Optional[bool] = None,
    allow_early_start: Optional[bool] = None,
    recurring: bool = False,
    recurring_rule: Optional[dict] = None,
    ext_attrs: Optional[list] = None,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    创建在线会议。POST /v7/meetings/create。
    start_time/end_time：建议带 Z（UTC）或 +08:00；无时区时按 UTC 解析，东8区用户易出现 8 小时偏差。
    """
    c = client or WpsV7Client()
    st = iso_to_timestamp(start_time)
    et = iso_to_timestamp(end_time)
    if not st or not et:
        raise ValueError("start_time 与 end_time 需为有效 ISO 8601（如 2026-03-02T08:00:00Z 或 2026-03-02T16:00:00+08:00）")
    if et <= st:
        raise ValueError("end_time 需晚于 start_time")

    booking = {"start_date_time": st, "end_date_time": et, "recurring": bool(recurring)}
    if recurring:
        if not recurring_rule or not isinstance(recurring_rule, dict):
            raise ValueError("recurring=true 时需提供 recurring_rule（dict）")
        booking["recurring_rule"] = recurring_rule

    settings = {}
    if approval_required_to_join is not None:
        settings["approval_required_to_join"] = bool(approval_required_to_join)
    if generate_summary is not None:
        settings["generate_summary"] = bool(generate_summary)
    if allow_early_start is not None:
        settings["allow_early_start"] = bool(allow_early_start)
    if allow_attendee_to_join is not None:
        settings["allow_attendee_to_join"] = allow_attendee_to_join
    if attendee_mic_status is not None:
        settings["attendee_mic_status"] = attendee_mic_status
    if attendee_camera_status is not None:
        settings["attendee_camera_status"] = attendee_camera_status

    body = {"subject": subject, "booking": booking}
    participants = _build_participants(participant_ids)
    if participants is not None:
        body["participants"] = participants
    if settings:
        body["settings"] = settings
    if ext_attrs is not None:
        body["ext_attrs"] = ext_attrs

    resp = c.post("/v7/meetings/create", json=body)
    return _normalize_resp(resp)


def update_meeting(
    meeting_id: str,
    *,
    subject: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    recurring: bool = False,
    recurring_rule: Optional[dict] = None,
    approval_required_to_join: Optional[bool] = None,
    allow_attendee_to_join: Optional[str] = None,
    attendee_mic_status: Optional[str] = None,
    attendee_camera_status: Optional[str] = None,
    generate_summary: Optional[bool] = None,
    allow_early_start: Optional[bool] = None,
    ext_attrs: Optional[list] = None,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """修改在线会议。POST /v7/meetings/{meeting_id}/update。"""
    c = client or WpsV7Client()
    body: dict = {}

    if subject is not None:
        body["subject"] = subject

    if (start_time is None) ^ (end_time is None):
        raise ValueError("修改时间需同时提供 start_time 与 end_time")
    if start_time is not None and end_time is not None:
        st = iso_to_timestamp(start_time)
        et = iso_to_timestamp(end_time)
        if not st or not et:
            raise ValueError("start_time 与 end_time 需为有效 ISO 8601（建议带 Z 或 +08:00）")
        if et <= st:
            raise ValueError("end_time 需晚于 start_time")
        booking = {"start_date_time": st, "end_date_time": et, "recurring": bool(recurring)}
        if recurring:
            if not recurring_rule or not isinstance(recurring_rule, dict):
                raise ValueError("recurring=true 时需提供 recurring_rule（dict）")
            booking["recurring_rule"] = recurring_rule
        body["booking"] = booking

    settings = {}
    if approval_required_to_join is not None:
        settings["approval_required_to_join"] = bool(approval_required_to_join)
    if generate_summary is not None:
        settings["generate_summary"] = bool(generate_summary)
    if allow_early_start is not None:
        settings["allow_early_start"] = bool(allow_early_start)
    if allow_attendee_to_join is not None:
        settings["allow_attendee_to_join"] = allow_attendee_to_join
    if attendee_mic_status is not None:
        settings["attendee_mic_status"] = attendee_mic_status
    if attendee_camera_status is not None:
        settings["attendee_camera_status"] = attendee_camera_status
    if settings:
        body["settings"] = settings

    if ext_attrs is not None:
        body["ext_attrs"] = ext_attrs

    if not body:
        raise ValueError("请至少指定一个可修改字段（如 subject 或 start_time/end_time 或 settings）")

    resp = c.post(f"/v7/meetings/{meeting_id}/update", json=body)
    return _normalize_resp(resp)


def delete_meeting(meeting_id: str, client: Optional[WpsV7Client] = None) -> dict:
    """取消会议。POST /v7/meetings/{meeting_id}/delete。"""
    c = client or WpsV7Client()
    resp = c.post(f"/v7/meetings/{meeting_id}/delete")
    return _normalize_resp(resp)


def get_meeting(meeting_id: str, client: Optional[WpsV7Client] = None) -> dict:
    """获取会议信息。GET /v7/meetings/{meeting_id}。"""
    c = client or WpsV7Client()
    resp = c.get(f"/v7/meetings/{meeting_id}")
    return _normalize_resp(resp)


def list_meetings(
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """获取会议列表。GET /v7/meetings。"""
    c = client or WpsV7Client()
    params = {}
    if (start_time is None) ^ (end_time is None):
        raise ValueError("查询列表需同时提供 start_time 与 end_time，或都不提供")
    if start_time is not None and end_time is not None:
        st = iso_to_timestamp(start_time)
        et = iso_to_timestamp(end_time)
        if not st or not et:
            raise ValueError("start_time 与 end_time 需为有效 ISO 8601（建议带 Z 或 +08:00）")
        params["start_date_time"] = st
        params["end_date_time"] = et
    resp = c.get("/v7/meetings", params=params or None)
    return _normalize_resp(resp)


def list_meeting_participants(
    meeting_id: str,
    page_token: Optional[str] = None,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """获取会议邀请成员列表。GET /v7/meetings/{meeting_id}/participants。"""
    c = client or WpsV7Client()
    params = {"page_token": page_token} if page_token else None
    resp = c.get(f"/v7/meetings/{meeting_id}/participants", params=params)
    return _normalize_resp(resp)


def put_meeting_participants(
    meeting_id: str,
    participant_ids: Iterable[str],
    *,
    role: str = "attendee",
    client: Optional[WpsV7Client] = None,
) -> dict:
    """添加会议邀请成员。POST /v7/meetings/{meeting_id}/participants/put。"""
    c = client or WpsV7Client()
    items = _build_participants(participant_ids, role=role) or []
    body = {"items": items}
    resp = c.post(f"/v7/meetings/{meeting_id}/participants/put", json=body)
    return _normalize_resp(resp)


def delete_meeting_participants(
    meeting_id: str,
    participant_ids: Iterable[str],
    client: Optional[WpsV7Client] = None,
) -> dict:
    """移除会议邀请成员。POST /v7/meetings/{meeting_id}/participants/delete。"""
    c = client or WpsV7Client()
    ids = [str(x) for x in participant_ids if x]
    body = {"items": ids}
    resp = c.post(f"/v7/meetings/{meeting_id}/participants/delete", json=body)
    return _normalize_resp(resp)


# ----- 会议室（internal：会议室层级、日程会议室） -----


def list_meeting_room_levels(
    room_level_id: Optional[str] = None,
    direct_access: Optional[bool] = None,
    page_size: Optional[int] = None,
    page_token: Optional[str] = None,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    管理员-查询会议室层级列表。
    GET /v7/admin/meeting_room_levels。
    room_level_id: 层级 ID，传空获取根下层级；可从本接口上一页获取。
    direct_access: 是否忽略中间过渡层级，为 true 则不返回从根到有权限层级之间的过渡层级。
    page_size: 每页数量，默认 20，最大 50。
    page_token: 分页凭证，首次不传。
    返回 data.items 为会议室层级列表（id, name, parent_id, path, name_path, has_child）。
    """
    c = client or WpsV7Client()
    params: dict = {}
    if room_level_id is not None:
        params["room_level_id"] = room_level_id
    if direct_access is not None:
        params["direct_access"] = direct_access
    if page_size is not None:
        params["page_size"] = min(50, max(1, page_size))
    if page_token:
        params["page_token"] = page_token
    resp = c.get("/v7/admin/meeting_room_levels", params=params or None)
    return _normalize_resp(resp)


def list_event_meeting_rooms(
    calendar_id: str,
    event_id: str,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    获取某个日程的会议室列表。
    GET /v7/calendars/{calendar_id}/events/{event_id}/meeting_rooms。
    calendar_id: 日历 ID，可从日历列表或主日历获取，或使用 primary。
    event_id: 日程 ID，可从日程列表获取。
    返回 data.items 为会议室列表（room_id, name, result, fail_reason）。
    """
    c = client or WpsV7Client()
    resp = c.get(
        f"/v7/calendars/{calendar_id}/events/{event_id}/meeting_rooms"
    )
    return _normalize_resp(resp)


# ----- 会议转写与会议总结 -----


def get_meeting_transcription(
    meeting_id: str,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    获取会议转写（语音转文字）结果。
    GET /v7/meetings/{meeting_id}/transcription。
    会议需已结束且已生成转写；返回 data 为转写内容（结构以服务端为准，如 segments/text 等）。
    """
    c = client or WpsV7Client()
    resp = c.get(f"/v7/meetings/{meeting_id}/transcription")
    return _normalize_resp(resp)


def get_meeting_summary(
    meeting_id: str,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    获取会议总结（AI 摘要）结果。
    GET /v7/meetings/{meeting_id}/summary。
    会议需已结束且已生成总结；返回 data 为总结内容（结构以服务端为准，如 summary/outline/action_items 等）。
    """
    c = client or WpsV7Client()
    resp = c.get(f"/v7/meetings/{meeting_id}/summary")
    return _normalize_resp(resp)


# ----- 已开始会议（started_meetings）：会议列表、录制、纪要 -----
# 参考 internal/meeting：list_started_meetings、meeting_get_recordings、get_recording_summary/transcript、
# meeting_get_minutes、get_minute_summary/transcript。


def list_started_meetings(
    start_time: str,
    end_time: str,
    join_code: Optional[str] = None,
    page_token: Optional[str] = None,
    page_size: int = 20,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    获取指定时间范围内已开始的会议简要列表（60 天内）。
    GET /v7/started_meetings。
    用户身份调用时可不传 join_code，表示查询当前用户最近参加的会议；若按入会码查则传 join_code。
    start_time/end_time：支持 ISO 8601 或 Unix 秒时间戳，接口使用 Unix 秒。
    返回 data.items 为会议简要列表（id, subject, join_code, join_url 等），data.next_page_token 为分页标记。
    """
    c = client or WpsV7Client()
    st = iso_to_timestamp(start_time)
    et = iso_to_timestamp(end_time)
    if st is None:
        st = start_time
    if et is None:
        et = end_time
    if not st or not et:
        raise ValueError("start_time 与 end_time 需为有效 ISO 8601 或 Unix 秒时间戳")
    params = {"start_time": str(st), "end_time": str(et)}
    if join_code is not None and str(join_code).strip():
        params["join_code"] = str(join_code).strip()
    if page_token:
        params["page_token"] = page_token
    params["page_size"] = min(50, max(1, page_size))
    resp = c.get("/v7/started_meetings", params=params)
    return _normalize_resp(resp)


def meeting_get_recordings(
    meeting_id: str,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    获取某场已开始会议的录制列表。
    GET /v7/started_meetings/{meeting_id}/recordings。
    返回 data.items 为录制列表（含 recording id 等），用于后续 get_recording_summary / get_recording_transcript。
    """
    c = client or WpsV7Client()
    resp = c.get(f"/v7/started_meetings/{meeting_id}/recordings")
    return _normalize_resp(resp)


def get_recording_summary(
    meeting_id: str,
    recording_id: str,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    获取指定录制的 AI 总结要点文本。
    GET /v7/started_meetings/{meeting_id}/recordings/{recording_id}/summary。
    返回 data.content 为总结文本。
    """
    c = client or WpsV7Client()
    resp = c.get(f"/v7/started_meetings/{meeting_id}/recordings/{recording_id}/summary")
    return _normalize_resp(resp)


def get_recording_transcript(
    meeting_id: str,
    recording_id: str,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    获取指定录制的语音转写文本。
    GET /v7/started_meetings/{meeting_id}/recordings/{recording_id}/transcript。
    返回 data 为转写内容（结构以服务端为准，如 meeting_transcript_content_json）。
    """
    c = client or WpsV7Client()
    resp = c.get(f"/v7/started_meetings/{meeting_id}/recordings/{recording_id}/transcript")
    return _normalize_resp(resp)


def meeting_get_minutes(
    meeting_id: str,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    获取某场已开始会议的纪要列表。
    GET /v7/started_meetings/{meeting_id}/minutes。
    返回 data.items 为纪要列表（含 minute id 等），用于后续 get_minute_summary / get_minute_transcript。
    """
    c = client or WpsV7Client()
    resp = c.get(f"/v7/started_meetings/{meeting_id}/minutes")
    return _normalize_resp(resp)


def get_minute_summary(
    meeting_id: str,
    minute_id: str,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    获取指定纪要的 AI 总结要点文本。
    GET /v7/started_meetings/{meeting_id}/minutes/{minute_id}/summary。
    返回 data.content 为总结文本。
    """
    c = client or WpsV7Client()
    resp = c.get(f"/v7/started_meetings/{meeting_id}/minutes/{minute_id}/summary")
    return _normalize_resp(resp)


def get_minute_transcript(
    meeting_id: str,
    minute_id: str,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    获取指定纪要的语音转写文本。
    GET /v7/started_meetings/{meeting_id}/minutes/{minute_id}/transcript。
    返回 data 为转写内容（结构以服务端为准，如 meeting_transcript_content_json）。
    """
    c = client or WpsV7Client()
    resp = c.get(f"/v7/started_meetings/{meeting_id}/minutes/{minute_id}/transcript")
    return _normalize_resp(resp)

