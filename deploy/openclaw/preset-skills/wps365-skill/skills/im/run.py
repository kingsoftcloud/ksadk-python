#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
聊天会话 IM：调用 V7 IM 接口，实现会话列表、消息、历史消息、发送消息等功能。
需在 wps365-skill 根目录执行，并设置环境变量 wps_sid。
用法:
  python skills/im/run.py list
  python skills/im/run.py get <chat_id>
  python skills/im/run.py history <chat_id>
  python skills/im/run.py send <chat_id> <text>     # 默认 text 类型并指定 markdown
  python skills/im/run.py send <chat_id> <text> --plain   # 以纯文本发送（不指定 markdown）
  python skills/im/run.py recall <chat_id> <message_id>
"""
import argparse
import json
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from wpsv7client import (
    get_chat_list,
    get_chat,
    list_chat_messages,
    has_at_tag,
    send_message,
    recall_message,
    list_recent_chats,
    search_chats,
    search_messages,
)


def _out(md_lines, data):
    lines = [""] + md_lines + ["", "## 原始数据 (JSON)", "", "```json", json.dumps(data, ensure_ascii=False, indent=2), "```"]
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


def _chat_display(chat):
    chat_type = chat.get("type", "-")
    name = chat.get("name", "-")
    chat_id = chat.get("id", "-")
    ctime = chat.get("ctime", "-")
    status = chat.get("status", "-")
    return f"[{chat_type}] {name} | ID: `{chat_id}` | 创建: {ctime} | 状态: {status}"


def cmd_list(args):
    resp = get_chat_list(page_size=args.page_size or 50)
    data = _check_resp(resp)
    items = data.get("items") or []
    md = ["## 会话列表", "", f"共 **{len(items)}** 个会话。"]
    if not items:
        md.append("")
        md.append("暂无会话。")
    else:
        md.append("")
        for i, chat in enumerate(items, 1):
            md.append(f"{i}. {_chat_display(chat)}")
    _out(md, data)


def cmd_get(args):
    if not args.chat_id:
        _err("请指定会话ID，例如: run.py get <chat_id>")
    resp = get_chat(args.chat_id)
    data = _check_resp(resp)
    chat = data
    md = ["## 会话详情", "", _chat_display(chat)]
    _out(md, data)


def cmd_history(args):
    if not args.chat_id:
        _err("请指定会话ID，例如: run.py history <chat_id>")
    resp = list_chat_messages(
        args.chat_id,
        page_size=args.page_size or 20,
        start_time=args.start_time,
        end_time=args.end_time,
        order=args.order or "by_create_time_desc",
    )
    data = _check_resp(resp)
    items = data.get("items") or []
    md = ["## 会话消息历史", "", f"会话 `{args.chat_id}` 共有 **{len(items)}** 条消息。"]
    if not items:
        md.append("")
        md.append("暂无消息。")
    else:
        md.append("")
        for i, msg in enumerate(items, 1):
            msg_type = msg.get("type", "-")
            msg_id = msg.get("id", "-")[:16] + "..." if len(msg.get("id", "")) > 16 else msg.get("id", "-")
            sender = msg.get("sender", {})
            sender_name = sender.get("name") or sender.get("id") or "-"
            ctime = msg.get("ctime", "-")
            content = msg.get("content", {})
            text = content.get("text", {}).get("content", "-") if content else "-"
            if text and len(text) > 50:
                text = text[:50] + "..."
            md.append(f"{i}. [{msg_type}] {sender_name} | {ctime}")
            if text != "-":
                md.append(f"   > {text}")
    _out(md, data)


def _parse_mentions(mention_list, zero_based=False):
    """
    将 --mention 参数列表解析为 API mentions 数组。
    支持 user_id 或 "all"（@所有人）。符合 internal/im schema v7_chat_message_mention。
    - id：与正文 <at id="N"> 匹配。zero_based=True 时为 "0","1",...（与 schema 示例一致）；否则为 "1","2",...
    - type：user | all；identity 仅 type=user 时必填。
    """
    if not mention_list:
        return None
    out = []
    for m in mention_list:
        m = (m or "").strip()
        if not m:
            continue
        index_str = str(len(out) if zero_based else len(out) + 1)
        if m.lower() == "all":
            out.append({"id": index_str, "type": "all"})
        else:
            out.append({
                "id": index_str,
                "type": "user",
                "identity": {"id": m, "type": "user"},
            })
    return out if out else None


def _content_and_mentions_to_zero_based(content, mentions):
    """
    将正文中的 <at id="1">、<at id="2"> 与 mentions 的 id 转为 0-based（schema 示例为 id="0"），
    便于客户端正确展示 @。返回 (new_content, new_mentions)。
    使用占位符避免先替换成较小数字时产生冲突（如 id="2"->"1" 后再 id="1"->"0" 会误改两处）。
    """
    if not content or not mentions:
        return content, mentions
    id_map = {m["id"]: str(i) for i, m in enumerate(mentions)}
    new_content = content
    placeholders = {}
    for old_id in sorted(id_map.keys(), key=lambda x: int(x), reverse=True):
        new_id = id_map[old_id]
        if old_id == new_id:
            continue
        ph = f"__AT_PH_{old_id}__"
        placeholders[ph] = new_id
        new_content = re.sub(r'<at\s+id="' + re.escape(old_id) + r'"', '<at id="' + ph + '"', new_content)
    for ph, new_id in placeholders.items():
        new_content = new_content.replace(ph, new_id)
    new_mentions = []
    for i, m in enumerate(mentions):
        item = {"id": str(i), "type": m["type"]}
        if m.get("identity"):
            item["identity"] = m["identity"]
        new_mentions.append(item)
    return new_content, new_mentions


def cmd_send(args):
    if not args.chat_id:
        _err("请指定会话ID，例如: run.py send <chat_id> <text>")
    if not args.msg_type:
        args.msg_type = "text"
    
    msg_type = args.msg_type.lower()
    use_plain_text = getattr(args, "plain", False)
    mentions = _parse_mentions(getattr(args, "mention", None) or [])

    if msg_type == "text":
        if not args.text:
            _err("请指定消息内容")
        text_type = None if use_plain_text else "markdown"
        if mentions and has_at_tag(args.text):
            if getattr(args, "at_markdown", False):
                # at + markdown：与「不 at」一致，发送 text + markdown + mentions（0-based），
                # 客户端按 text 的 markdown 渲染，同时根据 mentions 展示 @
                content_0, mentions_0 = _content_and_mentions_to_zero_based(args.text, mentions)
                resp = send_message(
                    chat_id=args.chat_id,
                    msg_type="text",
                    text=content_0,
                    text_type="markdown",
                    mentions=mentions_0,
                )
            else:
                # at 仅 plain：text + plain + 0-based，保证 @ 展示
                content_0, mentions_0 = _content_and_mentions_to_zero_based(args.text, mentions)
                resp = send_message(
                    chat_id=args.chat_id,
                    msg_type="text",
                    text=content_0,
                    text_type="plain",
                    mentions=mentions_0,
                )
        else:
            resp = send_message(
                chat_id=args.chat_id,
                msg_type="text",
                text=args.text,
                text_type=text_type,
                mentions=mentions,
            )
    elif msg_type == "rich_text":
        if not args.rich_text:
            _err("请指定富文本内容 (--rich-text)")
        try:
            rich_text_data = json.loads(args.rich_text)
        except json.JSONDecodeError:
            _err("富文本内容必须是有效的 JSON 格式")
        resp = send_message(
            chat_id=args.chat_id,
            msg_type="rich_text",
            rich_text=rich_text_data,
            mentions=mentions,
        )
    elif msg_type == "image":
        if not args.image_key:
            _err("请指定图片存储key (--image-key)")
        try:
            image_data = json.loads(args.image_key)
        except json.JSONDecodeError:
            _err("图片内容必须是有效的 JSON 格式")
        resp = send_message(
            chat_id=args.chat_id,
            msg_type="image",
            image=image_data,
            mentions=mentions,
        )
    elif msg_type == "card":
        if not args.card:
            _err("请指定卡片内容 (--card)")
        try:
            card_data = json.loads(args.card)
        except json.JSONDecodeError:
            _err("卡片内容必须是有效的 JSON 格式")
        resp = send_message(
            chat_id=args.chat_id,
            msg_type="card",
            card=card_data,
            mentions=mentions,
        )
    elif msg_type == "file":
        if not args.file:
            _err("请指定文件内容 (--file)")
        try:
            file_data = json.loads(args.file)
        except json.JSONDecodeError:
            _err("文件内容必须是有效的 JSON 格式")
        
        # 自动处理云文档：如果有 cloud 类型且有 link_id，自动将 link_id 设置为 id
        if file_data.get("type") == "cloud" and "cloud" in file_data:
            cloud_data = file_data.get("cloud", {})
            if cloud_data.get("link_id") and not cloud_data.get("id"):
                cloud_data["id"] = cloud_data["link_id"]
        
        resp = send_message(
            chat_id=args.chat_id,
            msg_type="file",
            file=file_data,
            mentions=mentions,
        )
    else:
        _err(f"不支持的消息类型: {msg_type}，支持的类型: text, rich_text, image, file, card")
    
    data = _check_resp(resp)
    msg = data
    md = ["## 发送消息", "", f"消息已发送至会话 `{args.chat_id}`。", ""]
    md.append(f"- 消息ID: `{msg.get('id', '-')}`")
    md.append(f"- 类型: {msg.get('type', '-')}")
    md.append(f"- 发送者: {msg.get('sender', {}).get('name', '-')}")
    _out(md, data)


def cmd_recall(args):
    if not args.chat_id:
        _err("请指定会话ID，例如: run.py recall <chat_id> <message_id>")
    if not args.message_id:
        _err("请指定消息ID，例如: run.py recall <chat_id> <message_id>")
    resp = recall_message(args.chat_id, args.message_id)
    _check_resp(resp)
    md = ["## 撤回消息", "", f"消息 `{args.message_id}` 已从会话 `{args.chat_id}` 撤回。"]
    _out(md, {"chat_id": args.chat_id, "message_id": args.message_id, "recalled": True})


def cmd_recent(args):
    resp = list_recent_chats(
        page_size=args.page_size or 50,
        start_time=args.start_time,
        end_time=args.end_time,
        filter_unread=args.filter_unread,
        filter_mention_me=args.filter_mention_me,
    )
    data = _check_resp(resp)
    items = data.get("items") or []
    md = ["## 最近会话", "", f"共 **{len(items)}** 个最近会话。"]
    if not items:
        md.append("")
        md.append("暂无最近会话。")
    else:
        md.append("")
        for i, item in enumerate(items, 1):
            chat = item.get("chat") or {}
            chat_type = chat.get("type", "-")
            name = chat.get("name", "-")
            chat_id = chat.get("id", "-")
            unread = item.get("unread_count", 0)
            md.append(f"{i}. [{chat_type}] {name} | ID: `{chat_id}` | 未读: {unread}")
    _out(md, data)


def cmd_search(args):
    if not args.keyword:
        _err("请指定搜索关键字，例如: run.py search \"关键词\"")
    resp = search_chats(
        keyword=args.keyword,
        page_size=args.page_size or 20,
        with_total=True,
    )
    data = _check_resp(resp)
    items = data.get("items") or []
    total = data.get("total", len(items))
    md = ["## 搜索会话", "", f"关键字「{args.keyword}」共找到 **{total}** 个会话。"]
    if not items:
        md.append("")
        md.append("未找到匹配会话。")
    else:
        md.append("")
        for i, item in enumerate(items, 1):
            chat = item.get("chat") or {}
            chat_type = chat.get("type", "-")
            name = chat.get("name", "-")
            chat_id = chat.get("id", "-")
            md.append(f"{i}. [{chat_type}] {name} | ID: `{chat_id}`")
    _out(md, data)


def cmd_search_messages(args):
    if not args.keyword and not args.chat_ids and not args.sender_ids and not args.start_time:
        _err("请指定搜索条件（关键字、会话ID列表、发送者ID列表或时间范围之一）")
    resp = search_messages(
        keyword=args.keyword,
        page_size=args.page_size or 20,
        chat_id_list=args.chat_ids.split(",") if args.chat_ids else None,
        sender_id_list=args.sender_ids.split(",") if args.sender_ids else None,
        filter_msg_tag_list=args.msg_types.split(",") if args.msg_types else None,
        start_time=args.start_time,
        end_time=args.end_time,
        order=args.order or "by_create_time_desc",
        with_sender_details=True,
        with_chat=True,
    )
    data = _check_resp(resp)
    items = data.get("items") or []
    md = ["## 全局搜索消息", "", f"共找到 **{len(items)}** 条消息。"]
    if not items:
        md.append("")
        md.append("未找到匹配消息。")
    else:
        md.append("")
        for i, item in enumerate(items, 1):
            chat = item.get("chat") or {}
            chat_name = chat.get("name", "-")
            chat_id = chat.get("id", "-")
            msg = item.get("message") or {}
            msg_type = msg.get("type", "-")
            msg_id = msg.get("id", "-")[:16] + "..." if len(msg.get("id", "")) > 16 else msg.get("id", "-")
            sender = msg.get("sender") or {}
            sender_name = sender.get("name") or sender.get("id") or "-"
            ctime = msg.get("ctime", "-")
            content = msg.get("content", {})
            text = content.get("text", {}).get("content", "-") if content else "-"
            if text and len(text) > 40:
                text = text[:40] + "..."
            md.append(f"{i}. [{msg_type}] {sender_name} | {ctime}")
            md.append(f"   > 会话: {chat_name} | ID: `{chat_id}`")
            if text != "-":
                md.append(f"   > {text}")
    _out(md, data)


def main():
    parser = argparse.ArgumentParser(description="聊天会话 IM（V7）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="获取当前用户会话列表")
    p.add_argument("--page-size", "-p", type=int, default=50, help="分页大小，默认50")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("get", help="获取指定会话详情")
    p.add_argument("chat_id", nargs="?", default=None, help="会话ID")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("history", help="获取会话历史消息")
    p.add_argument("chat_id", nargs="?", default=None, help="会话ID")
    p.add_argument("--page-size", "-p", type=int, default=20, help="分页大小，默认20")
    p.add_argument("--start-time", "-s", default=None, help="起始时间，UTC ISO 8601 格式")
    p.add_argument("--end-time", "-e", default=None, help="结束时间，UTC ISO 8601 格式")
    p.add_argument("--order", "-o", default="by_create_time_desc", help="排序方式: by_create_time_desc 或 by_create_time_asc")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("send", help="发送消息到指定会话 (默认 text+markdown；可选纯文本/富文本/图片/文件/卡片)")
    p.add_argument("chat_id", nargs="?", default=None, help="会话ID")
    p.add_argument("text", nargs="?", default=None, help="消息内容（默认以 text+markdown 发送）")
    p.add_argument("--type", "-t", dest="msg_type", default="text", help="消息类型: text, rich_text, image, file, card (默认 text，即 text+markdown)")
    p.add_argument("--plain", "-P", action="store_true", help="仅当 type=text 时有效：以纯文本发送，不指定 markdown")
    p.add_argument("--rich-text", "-r", default=None, help="富文本内容 (JSON 格式)")
    p.add_argument("--image-key", "-i", default=None, help="图片存储key (图片消息需要)")
    p.add_argument("--card", "-c", default=None, help="卡片消息内容 (JSON 格式)")
    p.add_argument("--file", "-f", default=None, help="文件消息内容 (JSON 格式，支持云文档)")
    p.add_argument("--mention", "-M", action="append", default=None, dest="mention", help="@某人：传 user_id；@所有人：传 all。正文须写闭合标签如 <at id=\"1\">展示名</at>，可多次")
    p.add_argument("--at-markdown", action="store_true", dest="at_markdown", help="与 --mention 同用时：以 rich_text 发送，文本段为 markdown、@ 为 mention 元素，以同时展示 @ 与 Markdown")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("recall", help="撤回指定消息")
    p.add_argument("chat_id", nargs="?", default=None, help="会话ID")
    p.add_argument("message_id", nargs="?", default=None, help="消息ID")
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("recent", help="获取最近会话列表")
    p.add_argument("--page-size", "-p", type=int, default=50, help="分页大小，默认50")
    p.add_argument("--start-time", "-s", default=None, help="起始时间，UTC ISO 8601 格式")
    p.add_argument("--end-time", "-e", default=None, help="结束时间，UTC ISO 8601 格式")
    p.add_argument("--filter-unread", action="store_true", help="只返回未读会话")
    p.add_argument("--filter-mention-me", action="store_true", help="只返回@我的会话")
    p.set_defaults(func=cmd_recent)

    p = sub.add_parser("search", help="搜索会话")
    p.add_argument("keyword", nargs="?", default=None, help="搜索关键字")
    p.add_argument("--page-size", "-p", type=int, default=20, help="分页大小，默认20")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("search-messages", help="全局搜索消息")
    p.add_argument("--keyword", "-k", default=None, help="搜索关键字")
    p.add_argument("--chat-ids", "-c", default=None, help="会话ID列表，逗号分隔")
    p.add_argument("--sender-ids", "-s", default=None, help="发送者ID列表，逗号分隔")
    p.add_argument("--msg-types", "-t", default=None, help="消息类型过滤，如 text,file,image,cloud_file")
    p.add_argument("--page-size", "-p", type=int, default=20, help="分页大小，默认20")
    p.add_argument("--start-time", default=None, help="起始时间，UTC ISO 8601 格式")
    p.add_argument("--end-time", default=None, help="结束时间，UTC ISO 8601 格式")
    p.add_argument("--order", "-o", default="by_create_time_desc", help="排序方式: by_create_time_desc 或 by_create_time_asc")
    p.set_defaults(func=cmd_search_messages)

    args = parser.parse_args()
    try:
        args.func(args)
    except ValueError as e:
        _err(str(e))
    except Exception as e:
        _err("请求失败: " + str(e))


if __name__ == "__main__":
    main()
