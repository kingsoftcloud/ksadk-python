#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日历与日程增删改查：调用 V7 日历/日程接口，输出 Markdown + JSON。
需在 wps365-skill 根目录执行，并设置环境变量 wps_sid。
用法: python skills/calendar/run.py <子命令> [参数...]
"""
import argparse
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from wpsv7client import (  # noqa: E402
    list_calendars,
    get_calendar,
    create_calendar,
    update_calendar,
    delete_calendar,
    list_events,
    get_event,
    list_free_busy,
    create_event,
    update_event,
    delete_event,
    batch_create_event_attendees,
    batch_delete_event_attendees,
)


def _out(md_lines, data):
    lines = [""] + md_lines + ["", "## 原始数据 (JSON)", "", "```json", json.dumps(data, ensure_ascii=False, indent=2), "```"]
    print("\n".join(lines))
    sys.stdout.flush()


def _err(msg):
    print("## 错误\n\n" + msg, file=sys.stderr)
    sys.exit(1)


# 日程更新时若内容无变化，接口返回此码，视为成功
EVENT_NO_CHANGE_CODE = 400411340


def _check_resp(resp, ignore_codes=None):
    """先判断 code 正常（或属于可忽略码），再返回 data。与用户信息 skill 一致。"""
    code = resp.get("code")
    if code == 0:
        d = resp.get("data")
        return d if d is not None else {}
    if ignore_codes and code in ignore_codes:
        return resp.get("data") if resp.get("data") is not None else {}
    _err(resp.get("msg") or resp.get("message") or "未知错误")


def cmd_list_calendars(_):
    resp = list_calendars()
    data = _check_resp(resp)
    items = data.get("items", []) if isinstance(data, dict) else []
    md = ["## 日历列表", "", f"共 {len(items)} 条（当前页）"]
    if items:
        md.append("")
        for c in items[:20]:
            md.append(f"- **{c.get('summary', '-')}** `{c.get('id', '')}`")
    _out(md, data)


def cmd_get_calendar(args):
    resp = get_calendar(args.calendar_id)
    data = _check_resp(resp)
    if not isinstance(data, dict):
        data = {}
    s = data.get("summary") or "-"
    md = ["## 日历详情", "", f"- **标题**：{s}", f"- **ID**：{data.get('id', '-')}", f"- **颜色**：{data.get('color', '-')}"]
    _out(md, data)


def cmd_create_calendar(args):
    resp = create_calendar(summary=args.title, color=args.color, description=args.desc or "")
    data = _check_resp(resp)
    if not isinstance(data, dict):
        data = {}
    md = ["## 已创建日历", "", f"- **标题**：{data.get('summary', '-')}", f"- **ID**：{data.get('id', '-')}"]
    _out(md, data)


def cmd_update_calendar(args):
    kwargs = {}
    if args.title is not None:
        kwargs["summary"] = args.title
    if args.color is not None:
        kwargs["color"] = args.color
    if args.desc is not None:
        kwargs["description"] = args.desc
    if not kwargs:
        _err("请至少指定 --title、--color 或 --desc 之一")
    resp = update_calendar(args.calendar_id, **kwargs)
    data = _check_resp(resp)
    if not isinstance(data, dict):
        data = {}
    md = ["## 已更新日历", "", f"日历 `{args.calendar_id}` 已更新。"]
    _out(md, data)


def cmd_delete_calendar(args):
    resp = delete_calendar(args.calendar_id)
    data = _check_resp(resp)
    if not isinstance(data, dict):
        data = {}
    md = ["## 已删除日历", "", f"日历 `{args.calendar_id}` 已删除。"]
    _out(md, data)


def cmd_list_events(args):
    resp = list_events(args.calendar_id, start_time=args.start, end_time=args.end)
    data = _check_resp(resp)
    items = data.get("items", []) if isinstance(data, dict) else []
    md = ["## 日程列表", "", f"时间区间：{args.start} ~ {args.end}", f"共 {len(items)} 条（当前页）"]
    if items:
        md.append("")
        for e in items[:30]:
            title = e.get("summary") or "(无标题)"
            eid = e.get("id", "")
            md.append(f"- **{title}** `{eid}`")
    _out(md, data)


def _format_free_busy_time(t):
    """忙闲接口返回的 start/end 可能是字符串或 dict。"""
    if t is None:
        return "-"
    if isinstance(t, dict):
        return t.get("datetime") or t.get("date") or str(t)
    return str(t)


def cmd_free_busy(args):
    user_ids = _split_ids(getattr(args, "user_ids", None) or "")
    room_ids = _split_ids(getattr(args, "room_ids", None) or "")
    if not user_ids and not room_ids:
        _err("请指定 --user-ids 或 --room-ids 至少其一（逗号分隔），查询时间区间不超过 7 天")
    resp = list_free_busy(
        start_time=args.start,
        end_time=args.end,
        user_ids=user_ids if user_ids else None,
        room_ids=room_ids if room_ids else None,
    )
    data = _check_resp(resp)
    items = data.get("items", []) if isinstance(data, dict) else []
    md = [
        "## 日程忙闲",
        "",
        f"时间区间：{args.start} ~ {args.end}（不超过 7 天）",
        "",
        "以下为**忙**时间段，区间内其余时间为**空闲**。",
        "",
    ]
    for item in items:
        uid = item.get("user_id") or item.get("room_id") or "-"
        kind = "用户" if item.get("user_id") else "会议室"
        md.append(f"### {kind} `{uid}`")
        busy = item.get("busy_times") or []
        if busy:
            for bt in busy[:20]:
                st = _format_free_busy_time(bt.get("start"))
                et = _format_free_busy_time(bt.get("end"))
                md.append(f"- 忙：{st} ~ {et}")
        else:
            md.append("- 该区间内无忙时段（全部空闲）")
        md.append("")
    _out(md, data)


def _format_calendar_time(t):
    if t is None:
        return "-"
    if isinstance(t, dict):
        return t.get("datetime") or t.get("date") or "-"
    return str(t)


def cmd_get_event(args):
    resp = get_event(args.calendar_id, args.event_id)
    data = _check_resp(resp)
    if not isinstance(data, dict):
        data = {}
    st = _format_calendar_time(data.get("start_time"))
    et = _format_calendar_time(data.get("end_time"))
    md = [
        "## 日程详情",
        "",
        f"- **标题**：{data.get('summary', '-')}",
        f"- **ID**：{data.get('id', '-')}",
        f"- **开始**：{st}",
        f"- **结束**：{et}",
    ]
    _out(md, data)


def _split_ids(s):
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


def cmd_create_event(args):
    attendee_ids = _split_ids(args.attendees) if getattr(args, "attendees", None) else None
    attach_ids = getattr(args, "attach", None) or []
    if isinstance(attach_ids, str):
        attach_ids = [attach_ids]
    resp = create_event(
        args.calendar_id,
        start_time=args.start,
        end_time=args.end,
        summary=args.title,
        description=args.desc,
        location=getattr(args, "location", None) or None,
        attachment_file_ids=attach_ids if attach_ids else None,
        attendee_user_ids=attendee_ids,
    )
    data = _check_resp(resp)
    if not isinstance(data, dict):
        data = {}
    md = [
        "## 已创建日程",
        "",
        f"- **标题**：{data.get('summary', '-')}",
        f"- **ID**：{data.get('id', '-')}",
    ]
    _out(md, data)


def cmd_update_event(args):
    kwargs = {}
    if args.title is not None:
        kwargs["summary"] = args.title
    if args.desc is not None:
        kwargs["description"] = args.desc
    if args.start is not None:
        kwargs["start_time"] = args.start
    if args.end is not None:
        kwargs["end_time"] = args.end
    if getattr(args, "location", None) is not None:
        kwargs["location"] = args.location
    attach = getattr(args, "attach", None)
    if attach is not None and (attach if isinstance(attach, list) else [attach]):
        kwargs["attachment_file_ids"] = attach if isinstance(attach, list) else [attach]
    attendee_ids = _split_ids(getattr(args, "attendees", None) or "")
    remove_attendee_ids = _split_ids(getattr(args, "remove_attendees", None) or "")
    if not kwargs and not attendee_ids and not remove_attendee_ids:
        _err("请至少指定 --title、--desc、--start、--end、--location、--attach、--attendees 或 --remove-attendees 之一")
    if kwargs:
        resp = update_event(args.calendar_id, args.event_id, **kwargs)
        data = _check_resp(resp, ignore_codes={EVENT_NO_CHANGE_CODE})
    else:
        data = {}
    if attendee_ids:
        add_resp = batch_create_event_attendees(args.calendar_id, args.event_id, attendee_ids)
        _check_resp(add_resp)
    if remove_attendee_ids:
        del_resp = batch_delete_event_attendees(args.calendar_id, args.event_id, remove_attendee_ids)
        _check_resp(del_resp)
    md = ["## 已更新日程", "", f"日程 `{args.event_id}` 已更新。"]
    _out(md, data)


def cmd_delete_event(args):
    resp = delete_event(args.calendar_id, args.event_id)
    data = _check_resp(resp)
    if not isinstance(data, dict):
        data = {}
    md = ["## 已删除日程", "", f"日程 `{args.event_id}` 已删除。"]
    _out(md, data)


def main():
    parser = argparse.ArgumentParser(description="日历与日程增删改查（V7）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # 日历
    p = sub.add_parser("list-calendars")
    p.set_defaults(func=cmd_list_calendars)
    p = sub.add_parser("get-calendar")
    p.add_argument("calendar_id")
    p.set_defaults(func=cmd_get_calendar)
    p = sub.add_parser("create-calendar")
    p.add_argument("--title", required=True, help="日历标题")
    p.add_argument("--color", required=True, help="颜色，如 #FF0000FF")
    p.add_argument("--desc", default="", help="描述")
    p.set_defaults(func=cmd_create_calendar)
    p = sub.add_parser("update-calendar")
    p.add_argument("calendar_id")
    p.add_argument("--title", default=None)
    p.add_argument("--color", default=None)
    p.add_argument("--desc", default=None)
    p.set_defaults(func=cmd_update_calendar)
    p = sub.add_parser("delete-calendar")
    p.add_argument("calendar_id")
    p.set_defaults(func=cmd_delete_calendar)

    # 日程
    p = sub.add_parser("list-events")
    p.add_argument("calendar_id")
    p.add_argument("--start", required=True, help="开始时间 ISO 8601")
    p.add_argument("--end", required=True, help="结束时间 ISO 8601")
    p.set_defaults(func=cmd_list_events)
    p = sub.add_parser("free-busy")
    p.add_argument("--start", required=True, help="开始时间 ISO 8601，区间不超过 7 天")
    p.add_argument("--end", required=True, help="结束时间 ISO 8601")
    p.add_argument("--user-ids", default=None, dest="user_ids", help="用户 id，逗号分隔，与 --room-ids 二选一或同时传")
    p.add_argument("--room-ids", default=None, dest="room_ids", help="会议室 id，逗号分隔")
    p.set_defaults(func=cmd_free_busy)
    p = sub.add_parser("get-event")
    p.add_argument("calendar_id")
    p.add_argument("event_id")
    p.set_defaults(func=cmd_get_event)
    p = sub.add_parser("create-event")
    p.add_argument("calendar_id")
    p.add_argument("--start", required=True, help="开始时间 ISO 8601 或 yyyy-mm-dd")
    p.add_argument("--end", required=True, help="结束时间 ISO 8601 或 yyyy-mm-dd")
    p.add_argument("--title", default=None)
    p.add_argument("--desc", default=None)
    p.add_argument("--location", default=None, help="地点名称，最多 1 个")
    p.add_argument("--attach", action="append", default=None, dest="attach", help="附件 file_id，可多次传入，最多 20 个")
    p.add_argument("--attendees", default=None, help="参与者 user_id，逗号分隔")
    p.set_defaults(func=cmd_create_event)
    p = sub.add_parser("update-event")
    p.add_argument("calendar_id")
    p.add_argument("event_id")
    p.add_argument("--title", default=None)
    p.add_argument("--desc", default=None)
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--location", default=None, help="地点名称")
    p.add_argument("--attach", action="append", default=None, dest="attach", help="附件 file_id，可多次传入")
    p.add_argument("--attendees", default=None, help="参与者 user_id，逗号分隔，将追加到日程")
    p.add_argument("--remove-attendees", default=None, dest="remove_attendees", help="要移除的参与者 id（从参与者列表获取），逗号分隔")
    p.set_defaults(func=cmd_update_event)
    p = sub.add_parser("delete-event")
    p.add_argument("calendar_id")
    p.add_argument("event_id")
    p.set_defaults(func=cmd_delete_event)

    args = parser.parse_args()
    try:
        args.func(args)
    except ValueError as e:
        _err(str(e))
    except Exception as e:
        _err("请求失败: " + str(e))


if __name__ == "__main__":
    main()
