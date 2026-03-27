# -*- coding: utf-8 -*-
"""
多维表（DbSheet）V7 API 封装。
参考 apis-dev/spec/internal/dbsheet：获取 Schema、列举/检索/创建/更新/删除记录，数据表与视图管理。
需 scope：app:kso.internal.dbsheet.read（只读）或 app:kso.internal.dbsheet.readwrite（读写）。
"""
from typing import Any, List, Optional

from .base import WpsV7Client


def _normalize_resp(resp: Any) -> dict:
    if resp is None:
        return {}
    if not isinstance(resp, dict):
        return resp if isinstance(resp, dict) else {}
    return dict(resp)


def get_schema(
    file_id: str,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    获取多维表 Schema（数据表、视图、字段结构）。
    GET /v7/dbsheet/{file_id}/schema
    file_id: 多维表文件 ID（云文档 file_id）。
    返回 data.sheets：数据表列表，每项含 id、name、views、fields、records_count 等。
    """
    c = client or WpsV7Client()
    resp = c.get(f"/v7/dbsheet/{file_id}/schema")
    return _normalize_resp(resp)


def list_records(
    file_id: str,
    sheet_id: int,
    *,
    prefer_id: Optional[bool] = None,
    text_value: Optional[str] = None,
    view_id: Optional[str] = None,
    max_records: Optional[int] = None,
    fields: Optional[List[str]] = None,
    filter_body: Optional[dict] = None,
    show_record_extra_info: Optional[bool] = None,
    show_fields_info: Optional[bool] = None,
    page_size: Optional[int] = None,
    page_token: Optional[str] = None,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    列举多维表记录（分页、筛选、指定字段）。
    POST /v7/dbsheet/{file_id}/sheets/{sheet_id}/records
    file_id: 文件 ID；sheet_id: 数据表 ID。
    text_value: original | text | compound。view_id: 从指定视图获取。max_records: 最多返回条数。
    fields: 要返回的字段名或字段 id 列表。filter_body: 筛选条件 { "mode": "AND"|"OR", "criteria": [...] }。
    """
    c = client or WpsV7Client()
    body: dict = {}
    if prefer_id is not None:
        body["prefer_id"] = prefer_id
    if text_value:
        body["text_value"] = text_value
    if view_id:
        body["view_id"] = view_id
    if max_records is not None:
        body["max_records"] = max_records
    if fields is not None:
        body["fields"] = fields
    if filter_body is not None:
        body["filter"] = filter_body
    if show_record_extra_info is not None:
        body["show_record_extra_info"] = show_record_extra_info
    if show_fields_info is not None:
        body["show_fields_info"] = show_fields_info
    if page_size is not None:
        body["page_size"] = min(1000, max(1, page_size))
    if page_token:
        body["page_token"] = page_token
    resp = c.post(
        f"/v7/dbsheet/{file_id}/sheets/{sheet_id}/records",
        json=body if body else {},
    )
    return _normalize_resp(resp)


def get_record(
    file_id: str,
    sheet_id: int,
    record_id: str,
    *,
    prefer_id: Optional[bool] = None,
    show_fields_info: Optional[bool] = None,
    show_record_extra_info: Optional[bool] = None,
    text_value: Optional[str] = None,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    检索单条记录。
    GET /v7/dbsheet/{file_id}/sheets/{sheet_id}/records/{record_id}
    """
    c = client or WpsV7Client()
    params: dict = {}
    if prefer_id is not None:
        params["prefer_id"] = prefer_id
    if show_fields_info is not None:
        params["show_fields_info"] = show_fields_info
    if show_record_extra_info is not None:
        params["show_record_extra_info"] = show_record_extra_info
    if text_value:
        params["text_value"] = text_value
    resp = c.get(
        f"/v7/dbsheet/{file_id}/sheets/{sheet_id}/records/{record_id}",
        params=params or None,
    )
    return _normalize_resp(resp)


def search_records(
    file_id: str,
    sheet_id: int,
    record_ids: List[str],
    *,
    prefer_id: Optional[bool] = None,
    show_fields_info: Optional[bool] = None,
    show_record_extra_info: Optional[bool] = None,
    text_value: Optional[str] = None,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    检索多条记录（按 record_id 列表）。
    POST /v7/dbsheet/{file_id}/sheets/{sheet_id}/records/search
    body.records: 记录 ID 列表。
    """
    c = client or WpsV7Client()
    body: dict = {"records": list(record_ids)}
    if prefer_id is not None:
        body["prefer_id"] = prefer_id
    if show_fields_info is not None:
        body["show_fields_info"] = show_fields_info
    if show_record_extra_info is not None:
        body["show_record_extra_info"] = show_record_extra_info
    if text_value:
        body["text_value"] = text_value
    resp = c.post(
        f"/v7/dbsheet/{file_id}/sheets/{sheet_id}/records/search",
        json=body,
    )
    return _normalize_resp(resp)


def batch_create_records(
    file_id: str,
    sheet_id: int,
    records: List[dict],
    *,
    prefer_id: Optional[bool] = None,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    批量创建记录。
    POST /v7/dbsheet/{file_id}/sheets/{sheet_id}/records/batch_create
    records: 列表，每项为 { "fields_value": "{\"字段名\": 值, ...}" } 或含 fields_value 的 raw json 字符串。
    """
    c = client or WpsV7Client()
    body: dict = {}
    if prefer_id is not None:
        body["prefer_id"] = prefer_id
    items = []
    for r in records:
        if isinstance(r, dict) and "fields_value" in r:
            items.append(r)
        elif isinstance(r, dict):
            import json
            items.append({"fields_value": json.dumps(r)})
        else:
            items.append({"fields_value": str(r)})
    body["records"] = items
    resp = c.post(
        f"/v7/dbsheet/{file_id}/sheets/{sheet_id}/records/batch_create",
        json=body,
    )
    return _normalize_resp(resp)


def batch_update_records(
    file_id: str,
    sheet_id: int,
    records: List[dict],
    *,
    prefer_id: Optional[bool] = None,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    批量更新记录。
    POST /v7/dbsheet/{file_id}/sheets/{sheet_id}/records/batch_update
    records: 列表，每项为 { "id": "记录ID", "fields_value": "{\"字段名\": 值}" }。
    """
    c = client or WpsV7Client()
    body: dict = {}
    if prefer_id is not None:
        body["prefer_id"] = prefer_id
    body["records"] = list(records)
    resp = c.post(
        f"/v7/dbsheet/{file_id}/sheets/{sheet_id}/records/batch_update",
        json=body,
    )
    return _normalize_resp(resp)


def batch_delete_records(
    file_id: str,
    sheet_id: int,
    record_ids: List[str],
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    批量删除记录。
    POST /v7/dbsheet/{file_id}/sheets/{sheet_id}/records/batch_delete
    record_ids: 记录 ID 列表。
    """
    c = client or WpsV7Client()
    resp = c.post(
        f"/v7/dbsheet/{file_id}/sheets/{sheet_id}/records/batch_delete",
        json={"records": list(record_ids)},
    )
    return _normalize_resp(resp)


def create_sheet(
    file_id: str,
    *,
    name: Optional[str] = None,
    fields: Optional[List[dict]] = None,
    views: Optional[List[dict]] = None,
    position: Optional[dict] = None,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    创建数据表。
    POST /v7/dbsheet/{file_id}/sheets/create
    name: 数据表名。fields: 字段列表（每项含 name、field_type、data 等）。views: 视图列表。position: { before_sheet_id?: int, after_sheet_id?: int }。
    """
    c = client or WpsV7Client()
    body: dict = {}
    if name is not None:
        body["name"] = name
    if fields is not None:
        body["fields"] = fields
    if views is not None:
        body["views"] = views
    if position is not None:
        body["position"] = position
    resp = c.post(f"/v7/dbsheet/{file_id}/sheets/create", json=body)
    return _normalize_resp(resp)


def update_sheet(
    file_id: str,
    sheet_id: int,
    *,
    name: Optional[str] = None,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    更新数据表（如改表名）。
    POST /v7/dbsheet/{file_id}/sheets/{sheet_id}/update
    """
    c = client or WpsV7Client()
    body = {} if name is None else {"name": name}
    resp = c.post(f"/v7/dbsheet/{file_id}/sheets/{sheet_id}/update", json=body)
    return _normalize_resp(resp)


def delete_sheet(
    file_id: str,
    sheet_id: int,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    删除数据表。
    POST /v7/dbsheet/{file_id}/sheets/{sheet_id}/delete
    """
    c = client or WpsV7Client()
    resp = c.post(f"/v7/dbsheet/{file_id}/sheets/{sheet_id}/delete")
    return _normalize_resp(resp)


def create_view(
    file_id: str,
    sheet_id: int,
    *,
    name: Optional[str] = None,
    view_type: Optional[str] = None,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    创建视图。
    POST /v7/dbsheet/{file_id}/sheets/{sheet_id}/views
    view_type: Grid | Kanban | Gallery | Form | Gantt | Query。
    """
    c = client or WpsV7Client()
    body: dict = {}
    if name is not None:
        body["name"] = name
    if view_type is not None:
        body["view_type"] = view_type
    resp = c.post(f"/v7/dbsheet/{file_id}/sheets/{sheet_id}/views", json=body)
    return _normalize_resp(resp)


def get_form_meta(
    file_id: str,
    sheet_id: int,
    view_id: str,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    获取表单视图元数据（名称、描述）。
    GET /v7/dbsheet/{file_id}/sheets/{sheet_id}/forms/{view_id}/meta
    """
    c = client or WpsV7Client()
    resp = c.get(f"/v7/dbsheet/{file_id}/sheets/{sheet_id}/forms/{view_id}/meta")
    return _normalize_resp(resp)


def update_form_meta(
    file_id: str,
    sheet_id: int,
    view_id: str,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    client: Optional[WpsV7Client] = None,
) -> dict:
    """
    更新表单视图元数据。
    POST /v7/dbsheet/{file_id}/sheets/{sheet_id}/forms/{view_id}/meta
    """
    c = client or WpsV7Client()
    body: dict = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    resp = c.post(
        f"/v7/dbsheet/{file_id}/sheets/{sheet_id}/forms/{view_id}/meta",
        json=body,
    )
    return _normalize_resp(resp)
