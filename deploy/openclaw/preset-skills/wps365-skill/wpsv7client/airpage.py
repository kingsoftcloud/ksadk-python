# -*- coding: utf-8 -*-
"""
智能文档 Airpage：365 内容 API 写入。
上传 Markdown 为 Airpage：先通过 drive 创建 .otl，再由此接口写入内容。
"""
from typing import Optional

from .base import WpsV7Client

CONTENT_API_BASE = "https://365.kdocs.cn"


def write_airpage_content(
    file_id: str,
    title: str,
    content: str,
    pos: str = "begin",
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    向智能文档写入内容（Markdown/文本）。调用 365 内容 API。
    POST https://365.kdocs.cn/api/v3/office/file/{file_id}/core/execute
    command: http.otl.exec, subType: insertContent.
    """
    c = client or WpsV7Client()
    url = f"{CONTENT_API_BASE}/api/v3/office/file/{file_id}/core/execute"
    body = {
        "command": "http.otl.exec",
        "param": {
            "subType": "insertContent",
            "params": {
                "title": title,
                "content": content,
                "pos": pos,
            },
        },
    }
    resp = c._session.post(url, json=body, headers=c._headers(), timeout=60)
    return resp.json() if resp.content else {}
