#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
会议创建/查询/取消与参会人管理：调用 V7 会议接口，输出 Markdown + JSON。
需在 wps365-skill 根目录执行，并设置环境变量 wps_sid。
用法: python skills/meeting/run.py <子命令> [参数...]
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
    create_meeting,
    update_meeting,
    delete_meeting,
    get_meeting,
    list_meetings,
    list_meeting_participants,
    put_meeting_participants,
    delete_meeting_participants,
    list_meeting_room_levels,
    list_event_meeting_rooms,
)


def _out(md_lines, data):
    lines = [
        "",
        *md_lines,
        "",
        "## 原始数据 (JSON)",
        "",
        "```json",
        json.dumps(data, ensure_ascii=False, indent=2),
        "```",
    ]
    print("\n".join(lines))
    sys.stdout.flush()


def _err(msg):
    print("## 错误\n\n" + msg, file=sys.stderr)
    sys.exit(1)


def _check_resp(resp):
    if resp.get("code") != 0:
        _err(resp.get("msg") or resp.get("message") or "未知错误")
    d = resp.get("data")
    return d if d is not None else {}


def _split_ids(s: str):
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


def _fmt_time_in_booking(m):
    b = (m or {}).get("booking") or {}
    st = b.get("start_date_time") or "-"
    et = b.get("end_date_time") or "-"
    return str(st), str(et)


def cmd_create(args):
    resp = create_meeting(
        subject=args.subject,
        start_time=args.start,
        end_time=args.end,
        participant_ids=_split_ids(args.participants) if args.participants else None,
        approval_required_to_join=args.approval_required if args.approval_required else None,
        generate_summary=args.generate_summary if args.generate_summary else None,
        allow_early_start=args.allow_early_start if args.allow_early_start else None,
        allow_attendee_to_join=args.join_permission,
    )
    data = _check_resp(resp)
    if not isinstance(data, dict):
        data = {}
    st, et = _fmt_time_in_booking(data)
    md = [
        "## 已创建会议",
        "",
        f"- **主题**：{data.get('subject', '-')}",
        f"- **会议ID**：{data.get('id', '-')}",
        f"- **时间**：{st} ~ {et}",
        f"- **入会码**：{data.get('join_code', '-')}",
        f"- **入会链接**：{data.get('join_url', '-')}",
    ]
    _out(md, data)


def cmd_get(args):
    resp = get_meeting(args.meeting_id)
    data = _check_resp(resp)
    if not isinstance(data, dict):
        data = {}
    st, et = _fmt_time_in_booking(data)
    md = [
        "## 会议详情",
        "",
        f"- **主题**：{data.get('subject', '-')}",
        f"- **会议ID**：{data.get('id', '-')}",
        f"- **时间**：{st} ~ {et}",
        f"- **入会码**：{data.get('join_code', '-')}",
        f"- **入会链接**：{data.get('join_url', '-')}",
    ]
    _out(md, data)


def cmd_list(args):
    resp = list_meetings(start_time=args.start, end_time=args.end)
    data = _check_resp(resp)
    items = data.get("items", []) if isinstance(data, dict) else []
    md = ["## 会议列表", "", f"时间区间：{args.start} ~ {args.end}", f"共 {len(items)} 条（当前页）"]
    if items:
        md.append("")
        for m in items[:30]:
            st, et = _fmt_time_in_booking(m)
            md.append(f"- **{m.get('subject') or '(无主题)'}** `{m.get('id', '')}`（{st} ~ {et}）")
    _out(md, data)


def cmd_update(args):
    resp = update_meeting(
        args.meeting_id,
        subject=args.subject,
        start_time=args.start,
        end_time=args.end,
        allow_attendee_to_join=args.join_permission,
        approval_required_to_join=args.approval_required if args.approval_required else None,
        generate_summary=args.generate_summary if args.generate_summary else None,
        allow_early_start=args.allow_early_start if args.allow_early_start else None,
    )
    data = _check_resp(resp)
    if not isinstance(data, dict):
        data = {}
    md = ["## 已修改会议", "", f"会议 `{args.meeting_id}` 已更新。"]
    _out(md, data)


def cmd_cancel(args):
    resp = delete_meeting(args.meeting_id)
    _check_resp(resp)
    _out(["## 已取消会议", "", f"会议 `{args.meeting_id}` 已取消。"], {})


def cmd_list_participants(args):
    resp = list_meeting_participants(args.meeting_id)
    data = _check_resp(resp)
    items = data.get("items", []) if isinstance(data, dict) else []
    md = ["## 参会人列表", "", f"会议 `{args.meeting_id}`：共 {len(items)} 人（当前页）"]
    if items:
        md.append("")
        for p in items[:50]:
            md.append(f"- **{p.get('nickname') or p.get('id') or '-'}** `{p.get('id', '')}`（{p.get('role', '-') }）")
    _out(md, data)


def cmd_add_participants(args):
    ids = _split_ids(args.ids)
    if not ids:
        _err("请通过 --ids 提供要邀请的参会人ID（逗号分隔）")
    resp = put_meeting_participants(args.meeting_id, ids, role="attendee")
    data = _check_resp(resp)
    _out(["## 已邀请参会人", "", f"会议 `{args.meeting_id}` 已添加 {len(ids)} 人。"], data)


def cmd_remove_participants(args):
    ids = _split_ids(args.ids)
    if not ids:
        _err("请通过 --ids 提供要移除的参会人ID（逗号分隔）")
    resp = delete_meeting_participants(args.meeting_id, ids)
    _check_resp(resp)
    _out(["## 已移除参会人", "", f"会议 `{args.meeting_id}` 已移除 {len(ids)} 人。"], {})


def cmd_list_room_levels(args):
    """管理员-会议室层级列表。"""
    resp = list_meeting_room_levels(
        room_level_id=getattr(args, "room_level_id", None) or None,
        page_size=getattr(args, "page_size", None),
        page_token=getattr(args, "page_token", None) or None,
    )
    data = _check_resp(resp)
    items = data.get("items", []) if isinstance(data, dict) else []
    next_token = (data or {}).get("next_page_token", "")
    md = ["## 会议室层级列表", ""]
    if getattr(args, "room_level_id", None):
        md.append(f"- **父层级ID**：`{args.room_level_id}`")
    md.append(f"- **本页**：{len(items)} 条")
    if next_token:
        md.append(f"- **下一页**：`--page-token {next_token}`")
    md.append("")
    for i, level in enumerate(items[:50], 1):
        name_path = level.get("name_path") or []
        path_str = " / ".join(name_path) if name_path else level.get("name", "-")
        md.append(f"{i}. **{level.get('name', '-')}** `{level.get('id', '')}` — {path_str}")
    _out(md, data)


def cmd_list_event_rooms(args):
    """某日程的会议室列表。"""
    calendar_id = getattr(args, "calendar_id", None) or ""
    event_id = getattr(args, "event_id", None) or ""
    if not calendar_id or not event_id:
        _err("请提供 calendar_id 与 event_id，例如: list-event-rooms primary <event_id>")
    resp = list_event_meeting_rooms(calendar_id=calendar_id, event_id=event_id)
    data = _check_resp(resp)
    items = data.get("items", []) if isinstance(data, dict) else []
    md = ["## 日程会议室列表", "", f"- **日历**：`{calendar_id}`", f"- **日程**：`{event_id}`", f"- **共 {len(items)} 个会议室**", ""]
    for i, room in enumerate(items[:30], 1):
        result = room.get("result", "-")
        reason = room.get("fail_reason", "")
        md.append(f"{i}. **{room.get('name', '-')}** `{room.get('room_id', '')}` — {result}" + (f"（{reason}）" if reason else ""))
    _out(md, data)


def main():
    parser = argparse.ArgumentParser(description="会议（V7）：创建/查询/取消与参会人管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create", help="创建预约会议（默认非周期）")
    p.add_argument("--subject", required=True, help="会议主题")
    p.add_argument("--start", required=True, help="开始时间 UTC ISO 8601")
    p.add_argument("--end", required=True, help="结束时间 UTC ISO 8601")
    p.add_argument("--participants", default=None, help="参会人ID列表（逗号分隔）")
    p.add_argument("--join-permission", default=None, choices=("anyone", "company_users", "only_invitee"))
    p.add_argument("--approval-required", action="store_true", help="开启入会审批")
    p.add_argument("--generate-summary", action="store_true", help="生成会后AI总结（如支持）")
    p.add_argument("--allow-early-start", action="store_true", help="允许提前开始")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("get", help="查看会议详情")
    p.add_argument("meeting_id")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("list", help="按时间范围列出会议")
    p.add_argument("--start", required=True, help="开始时间 UTC ISO 8601")
    p.add_argument("--end", required=True, help="结束时间 UTC ISO 8601")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("update", help="修改会议")
    p.add_argument("meeting_id")
    p.add_argument("--subject", default=None)
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--join-permission", default=None, choices=("anyone", "company_users", "only_invitee"))
    p.add_argument("--approval-required", action="store_true")
    p.add_argument("--generate-summary", action="store_true")
    p.add_argument("--allow-early-start", action="store_true")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("cancel", help="取消会议")
    p.add_argument("meeting_id")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("list-participants", help="参会人列表")
    p.add_argument("meeting_id")
    p.set_defaults(func=cmd_list_participants)

    p = sub.add_parser("add-participants", help="邀请参会人")
    p.add_argument("meeting_id")
    p.add_argument("--ids", required=True, help="参会人ID列表（逗号分隔）")
    p.set_defaults(func=cmd_add_participants)

    p = sub.add_parser("remove-participants", help="移除参会人")
    p.add_argument("meeting_id")
    p.add_argument("--ids", required=True, help="参会人ID列表（逗号分隔）")
    p.set_defaults(func=cmd_remove_participants)

    p = sub.add_parser("list-room-levels", help="管理员-会议室层级列表")
    p.add_argument("--room-level-id", default=None, help="层级ID，不传则从根下开始")
    p.add_argument("--page-size", type=int, default=None, help="每页数量，默认20，最大50")
    p.add_argument("--page-token", default=None, help="分页凭证")
    p.set_defaults(func=cmd_list_room_levels)

    p = sub.add_parser("list-event-rooms", help="某日程的会议室列表")
    p.add_argument("calendar_id", help="日历ID，可用 primary 表示主日历")
    p.add_argument("event_id", help="日程ID")
    p.set_defaults(func=cmd_list_event_rooms)

    args = parser.parse_args()
    try:
        args.func(args)
    except ValueError as e:
        _err(str(e))
    except Exception as e:
        _err("请求失败: " + str(e))


if __name__ == "__main__":
    main()

