# -*- coding: utf-8 -*-
"""
IM V7 API 封装。
参考 apis-dev/spec/internal/im：会话管理和消息管理接口。
"""

import re
from typing import Any, List, Optional, Tuple

from .base import WpsV7Client
from .time_util import response_timestamps_to_iso, iso_to_timestamp

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


def get_chat_list(
    client: Optional[WpsV7Client] = None,
    page_size: int = 50,
    page_token: Optional[str] = None,
) -> dict:
    """
    获取用户会话列表。
    GET /v7/chats。
    page_size: 分页大小，默认50，最大100
    page_token: 分页token
    """
    c = client or WpsV7Client()
    params = {
        "page_size": min(100, max(1, page_size)),
    }
    if page_token:
        params["page_token"] = page_token
    resp = c.get("/v7/chats", params=params)
    return _normalize_resp(resp)


def get_chat(
    chat_id: str,
    client: Optional[WpsV7Client] = None,
    with_group_ext_attrs: bool = False,
    with_p2p_ext_attrs: bool = False,
    with_ext_attrs: bool = False,
    with_tag: bool = False,
) -> dict:
    """
    获取会话信息。
    GET /v7/chats/{chat_id}。
    chat_id: 会话ID
    with_group_ext_attrs: 是否返回群扩展属性
    with_p2p_ext_attrs: 是否返回单聊扩展属性
    with_ext_attrs: 是否返回扩展属性
    with_tag: 是否返回会话标识
    """
    c = client or WpsV7Client()
    params = {}
    if with_group_ext_attrs:
        params["with_group_ext_attrs"] = True
    if with_p2p_ext_attrs:
        params["with_p2p_ext_attrs"] = True
    if with_ext_attrs:
        params["with_ext_attrs"] = True
    if with_tag:
        params["with_tag"] = True
    resp = c.get(f"/v7/chats/{chat_id}", params=params if params else None)
    return _normalize_resp(resp)


def list_chat_messages(
    chat_id: str,
    client: Optional[WpsV7Client] = None,
    page_size: int = 20,
    page_token: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    order: str = "by_create_time_desc",
    filter_unread: bool = False,
    filter_mention_me: bool = False,
    with_sender_details: bool = False,
) -> dict:
    """
    获取会话历史消息列表。
    GET /v7/chats/{chat_id}/messages。
    chat_id: 会话ID
    page_size: 分页大小，默认20，最大50
    page_token: 分页token
    start_time: 历史消息起始时间，UTC ISO 8601 格式（如 2024-01-01T00:00:00Z）
    end_time: 历史消息结束时间，UTC ISO 8601 格式
    order: 排序方式，by_create_time_desc（降序）或 by_create_time_asc（升序）
    filter_unread: 只返回未读消息
    filter_mention_me: 只返回@我的消息
    with_sender_details: 返回发送者详情
    """
    c = client or WpsV7Client()
    params = {
        "page_size": min(50, max(1, page_size)),
        "order": order,
    }
    if page_token:
        params["page_token"] = page_token
    if start_time:
        ts = iso_to_timestamp(start_time)
        if ts:
            params["start_time"] = ts
    if end_time:
        ts = iso_to_timestamp(end_time)
        if ts:
            params["end_time"] = ts
    if filter_unread:
        params["filter_unread"] = True
    if filter_mention_me:
        params["filter_mention_me"] = True
    if with_sender_details:
        params["with_sender_details"] = True
    resp = c.get(f"/v7/chats/{chat_id}/messages", params=params)
    return _normalize_resp(resp)


def card_from_markdown(
    content: str,
    lang_key: str = "zh-CN",
) -> dict:
    """
    将 Markdown 文本组装为 IM 卡片消息体（单块 markdown 文本）。
    用于发送时作为 send_message(..., msg_type="card", card=...) 的 card 参数。

    卡片消息组装参考（与 content.card 结构一致）:
        "content": {
            "card": {
                "i18n_items": [
                    {
                        "key": "zh-CN",
                        "value": {
                            "elements": [
                                {
                                    "text": {
                                        "tag": "text",
                                        "text": {
                                            "type": "markdown",
                                            "content": text
                                        }
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        }
    本函数返回上述 card 对象（即 content.card），不含外层 content。
    """
    return {
        "i18n_items": [
            {
                "key": lang_key,
                "value": {
                    "elements": [
                        {
                            "text": {
                                "tag": "text",
                                "text": {
                                    "type": "markdown",
                                    "content": content,
                                },
                            },
                        },
                    ],
                },
            },
        ],
    }


# 匹配 <at id="N">展示名</at>，N 为数字，展示名可含任意字符（非贪婪）
_AT_TAG_PATTERN = re.compile(r'<at\s+id="(\d+)"[^>]*>(.*?)</at>', re.DOTALL)


def _markdown_inline_to_style_runs(text: str) -> List[Tuple[str, dict]]:
    """
    将行内 Markdown（**加粗**、*斜体*）解析为 (纯文本, style) 的列表，
    供 rich_text 的 text 元素使用；客户端按 style 渲染，不依赖 type:markdown。
    """
    if not text or not text.strip():
        return [(text, {"bold": False, "italic": False, "color": "#000000FF"})] if text else []
    runs: List[Tuple[str, dict]] = []
    # 先按 **...** 切分，奇数段为加粗
    bold_parts = re.split(r"\*\*(.+?)\*\*", text)
    for i, part in enumerate(bold_parts):
        if part == "":
            continue
        is_bold = i % 2 == 1
        # 再按 *...*（单星，内容不含 *）切分，奇数段为斜体
        italic_parts = re.split(r"\*([^*]+)\*", part)
        for j, sub in enumerate(italic_parts):
            if sub == "":
                continue
            is_italic = j % 2 == 1
            style = {"bold": is_bold, "italic": is_italic, "color": "#000000FF"}
            runs.append((sub, style))
    if not runs:
        return [(text, {"bold": False, "italic": False, "color": "#000000FF"})]
    return runs


def has_at_tag(content: str) -> bool:
    """正文是否包含 <at id="N">...</at> 标签，用于决定是否用 rich_text 发送。"""
    return bool(content and _AT_TAG_PATTERN.search(content))


def rich_text_from_markdown_with_mentions(
    content: str,
    mentions: List[dict],
    text_type: str = "markdown",
) -> dict:
    """
    将含 <at id="1">展示名</at> 的正文解析为富文本 elements，用于发送 rich_text 类型消息。
    符合 schema_message_rich_text.xidl：text 元素用 style_content（text、style）；可选传 type:"markdown" 扩展；
    mention 元素用 mention_content（type、identity?、text）。id 未在 mentions 中的 at 标签按原文保留为 text。
    mentions 为 API 格式 [{"id":"1","type":"user","identity":{...}}, ...]，与正文 at id 对应。
    """
    mention_by_id = {str(m["id"]): m for m in (mentions or [])}
    elements: List[dict] = []
    idx = 0
    last_end = 0

    def _append_text_runs(seg: str) -> None:
        nonlocal idx
        if text_type == "markdown":
            runs = _markdown_inline_to_style_runs(seg)
            if not runs:
                runs = [(seg, {"bold": False, "italic": False, "color": "#000000FF"})]
            for run_text, style in runs:
                el = {
                    "type": "text",
                    "indent": 0,
                    "index": idx,
                    "alt_text": (run_text[:50] + "…") if len(run_text) > 50 else run_text,
                    "style_content": {"text": run_text, "style": style},
                }
                elements.append(el)
                idx += 1
        else:
            style = {"bold": False, "italic": False, "color": "#000000FF"}
            el = {
                "type": "text",
                "indent": 0,
                "index": idx,
                "alt_text": (seg[:50] + "…") if len(seg) > 50 else seg,
                "style_content": {"text": seg, "style": style},
            }
            elements.append(el)
            idx += 1

    for m in _AT_TAG_PATTERN.finditer(content):
        # 前面的文本片段
        text_seg = content[last_end : m.start()]
        if text_seg:
            _append_text_runs(text_seg)
        at_id = m.group(1)
        display_name = m.group(2).strip()
        mention = mention_by_id.get(at_id)
        if mention:
            # 严格符合 v7_message_rich_text_mention_content：type 必填；identity 可选，当 at 所有人时为空；text 必填
            mention_content = {
                "type": mention["type"],
                "text": display_name or ("所有人" if mention["type"] == "all" else ""),
            }
            if mention["type"] == "user" and mention.get("identity"):
                mention_content["identity"] = mention["identity"]
            mention_el = {
                "type": "mention",
                "indent": 0,
                "index": idx,
                "alt_text": display_name or at_id,
                "mention_content": mention_content,
            }
            elements.append(mention_el)
            idx += 1
        else:
            # id 未在 mentions 中，整段 at 标签按原文保留为 text 元素，避免丢失
            raw_at = content[m.start() : m.end()]
            _append_text_runs(raw_at)
        last_end = m.end()
    # 末尾文本
    text_seg = content[last_end:]
    if text_seg:
        _append_text_runs(text_seg)
    if not elements:
        # 无 at 或无有效内容时，整段作为一条 text 元素
        _append_text_runs(content)
    return {"elements": elements}


def send_message(
    chat_id: str,
    msg_type: str = "text",
    client: Optional[WpsV7Client] = None,
    biz_uuid: Optional[str] = None,
    text: Optional[str] = None,
    text_type: Optional[str] = None,
    rich_text: Optional[dict] = None,
    image: Optional[dict] = None,
    file: Optional[dict] = None,
    card: Optional[dict] = None,
    mentions: Optional[list] = None,
    quote_msg_id: Optional[str] = None,
) -> dict:
    """
    发送会话消息。
    POST /v7/chats/{chat_id}/messages/create。
    chat_id: 会话ID
    msg_type: 消息类型，如 text, rich_text, image, file, card 等
    biz_uuid: 业务方唯一字符串，用于幂等
    text: 文本消息内容
    text_type: 文本类型，传 "markdown" 时请求体为 text: { type: "markdown", content }，不传或 "plain" 为 text: { content }
    rich_text: 富文本消息内容，格式 {"elements": [...]}
    image: 图片消息内容，格式 {"storage_key": "xxx", "type": "image/jpg", "width": xxx, "height": xxx, "size": xxx, "name": "xxx"}
    file: 文件消息内容，格式:
      - 本地文件: {"type": "local", "local": {"storage_key": "xxx", "size": xxx, "name": "xxx"}}
      - 云文档: {"type": "cloud", "cloud": {"id": "xxx", "link_url": "xxx", "link_id": "xxx"}}
    card: 卡片消息内容
    mentions: 被@的人员列表，符合 v7_chat_message_mention。正文须为闭合标签且含展示名，例：你好<at id="1">张三</at>（schema_message.xidl）；id 从 1 开始，与 <at id="1"> 对应；@某人需带 identity。
    quote_msg_id: 被引用的消息ID
    """
    c = client or WpsV7Client()
    body = {
        "type": msg_type,
    }
    if biz_uuid:
        body["biz_uuid"] = biz_uuid
    if text is not None:
        if text_type == "markdown":
            body["text"] = {"type": "markdown", "content": text}
        else:
            body["text"] = {"content": text}
    if rich_text:
        body["rich_text"] = rich_text
    if image:
        body["image"] = image
    if file:
        body["file"] = file
    if card:
        body["card"] = card
    if mentions:
        body["mentions"] = mentions
    if quote_msg_id:
        body["quote_msg_id"] = quote_msg_id
    resp = c.post(f"/v7/chats/{chat_id}/messages/create", json=body)
    return _normalize_resp(resp)


def recall_message(
    chat_id: str,
    message_id: str,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    撤回会话消息。
    POST /v7/chats/{chat_id}/messages/{message_id}/recall。
    chat_id: 会话ID
    message_id: 消息ID
    """
    c = client or WpsV7Client()
    resp = c.post(f"/v7/chats/{chat_id}/messages/{message_id}/recall", json={})
    return _normalize_resp(resp)


def list_recent_chats(
    client: Optional[WpsV7Client] = None,
    page_size: int = 50,
    page_token: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    with_chat: bool = True,
    with_unread_count: bool = True,
    filter_unread: bool = False,
    filter_mention_me: bool = False,
    filter_normal_notification: bool = False,
) -> dict:
    """
    获取最近会话列表。
    GET /v7/recent_chats。
    page_size: 分页大小，默认50，最大100
    page_token: 分页token
    start_time: 起始时间，UTC ISO 8601 格式
    end_time: 结束时间，UTC ISO 8601 格式
    with_chat: 是否展开会话信息
    with_unread_count: 是否展开未读数
    filter_unread: 只返回未读会话
    filter_mention_me: 只返回@我的会话
    filter_normal_notification: 只返回可正常通知的会话
    """
    c = client or WpsV7Client()
    params = {
        "page_size": min(100, max(1, page_size)),
        "with_chat": with_chat,
        "with_unread_count": with_unread_count,
    }
    if page_token:
        params["page_token"] = page_token
    if start_time:
        ts = iso_to_timestamp(start_time)
        if ts:
            params["start_time"] = ts
    if end_time:
        ts = iso_to_timestamp(end_time)
        if ts:
            params["end_time"] = ts
    if filter_unread:
        params["filter_unread"] = True
    if filter_mention_me:
        params["filter_mention_me"] = True
    if filter_normal_notification:
        params["filter_normal_notification"] = True
    resp = c.get("/v7/recent_chats", params=params)
    return _normalize_resp(resp)


def search_chats(
    keyword: str,
    client: Optional[WpsV7Client] = None,
    page_size: int = 20,
    page_token: Optional[str] = None,
    with_total: bool = False,
    with_group_ext_attrs: bool = False,
) -> dict:
    """
    搜索会话。
    GET /v7/chats/search。
    keyword: 搜索关键字
    page_size: 分页大小，默认20，最大50
    page_token: 分页token
    with_total: 是否返回总数
    with_group_ext_attrs: 是否返回群聊额外信息
    """
    if not keyword or not str(keyword).strip():
        return _normalize_resp({"code": 0, "msg": "", "data": {"items": [], "total": 0}})
    c = client or WpsV7Client()
    params = {
        "keyword": str(keyword).strip(),
        "page_size": min(50, max(1, page_size)),
    }
    if page_token:
        params["page_token"] = page_token
    if with_total:
        params["with_total"] = True
    if with_group_ext_attrs:
        params["with_group_ext_attrs"] = True
    resp = c.get("/v7/chats/search", params=params)
    return _normalize_resp(resp)


def search_messages(
    keyword: Optional[str] = None,
    client: Optional[WpsV7Client] = None,
    page_size: int = 20,
    page_token: Optional[str] = None,
    chat_id_list: Optional[list] = None,
    filter_chat_type_list: Optional[list] = None,
    msg_type_list: Optional[list] = None,
    filter_msg_tag_list: Optional[list] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    sender_id_list: Optional[list] = None,
    order: str = "by_create_time_desc",
    filter_unread: bool = False,
    with_sender_details: bool = False,
    with_chat: bool = True,
) -> dict:
    """
    全局搜索消息。
    GET /v7/messages/search。
    keyword: 搜索关键字，会话ID或ID列表，发送者ID或ID列表，start_time/end_time时间范围四者必传其一
    page_size: 分页大小，默认20，最大50
    page_token: 分页token
    chat_id_list: 会话ID列表（最多50个）
    filter_chat_type_list: 会话类型过滤，如 ["group", "p2p"]
    msg_type_list: 消息类型过滤
    filter_msg_tag_list: 消息内容标签过滤，如 ["text", "file", "image", "cloud_file"]
    start_time: 起始时间，UTC ISO 8601 格式
    end_time: 结束时间，UTC ISO 8601 格式
    sender_id_list: 消息发送者ID列表（最多50个）
    order: 排序方式，by_create_time_desc（降序）或 by_create_time_asc（升序）
    filter_unread: 只返回未读消息
    with_sender_details: 返回发送者详情
    with_chat: 展开会话信息
    """
    c = client or WpsV7Client()
    params = {
        "page_size": min(50, max(1, page_size)),
        "with_chat": with_chat,
    }
    if page_token:
        params["page_token"] = page_token
    if keyword:
        params["keyword"] = str(keyword).strip()
    if chat_id_list:
        params["chat_id_list"] = chat_id_list[:50]
    if filter_chat_type_list:
        params["filter_chat_type_list"] = filter_chat_type_list
    if msg_type_list:
        params["msg_type_list"] = msg_type_list
    if filter_msg_tag_list:
        params["filter_msg_tag_list"] = filter_msg_tag_list
    if start_time:
        ts = iso_to_timestamp(start_time)
        if ts:
            params["start_time"] = ts
    if end_time:
        ts = iso_to_timestamp(end_time)
        if ts:
            params["end_time"] = ts
    if sender_id_list:
        params["sender_id_list"] = sender_id_list[:50]
    if order:
        params["order"] = order
    if filter_unread:
        params["filter_unread"] = True
    if with_sender_details:
        params["with_sender_details"] = True
    
    resp = c.get("/v7/messages/search", params=params)
    return _normalize_resp(resp)


def create_chat(
    account_id_list: List[str],
    chat_type: str = "p2p",
    name: Optional[str] = None,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    创建会话（单聊或群聊）。
    POST /v7/chats/create。
    account_id_list: 成员 user_id 列表。单聊时长度为 2（当前用户 + 对方），群聊时可为 1～100。
    chat_type: p2p（单聊）或 group（群聊）。
    name: 群聊名称（仅群聊可选）。
    返回 data 含 chat 信息（含 id 即 chat_id）。
    """
    c = client or WpsV7Client()
    body = {
        "type": chat_type,
        "account_id_list": [str(x) for x in account_id_list if x],
    }
    if name:
        body["name"] = name
    resp = c.post("/v7/chats/create", json=body)
    return _normalize_resp(resp)


def send_cloud_file(
    chat_id: str,
    file_id: str,
    link_url: str,
    link_id: str,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    发送云文档消息。
    POST /open/v7/chats/{chat_id}/messages/send_cloud_file。
    chat_id: 会话ID
    file_id: 云文档ID（或 link_id，用于展示）
    link_url: 云文档链接
    link_id: 云文档链接ID
    """
    c = client or WpsV7Client()
    body = {
        "file": {
            "type": "cloud",
            "cloud": {
                "id": file_id,
                "link_url": link_url,
                "link_id": link_id,
            }
        }
    }
    resp = c.post(f"/open/v7/chats/{chat_id}/messages/send_cloud_file", json=body)
    return _normalize_resp(resp)
