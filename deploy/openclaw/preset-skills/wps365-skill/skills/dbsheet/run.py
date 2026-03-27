#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多维表（DbSheet）：获取 Schema、列举/检索/创建/更新/删除记录。调用 V7 多维表接口，输出 Markdown + JSON。
需在 wps365-skill 根目录执行，并设置环境变量 wps_sid。
用法: python skills/dbsheet/run.py <子命令> [参数...]
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

from wpsv7client import (
    dbsheet_get_schema,
    dbsheet_list_records,
    dbsheet_get_record,
    dbsheet_search_records,
    dbsheet_batch_create_records,
    dbsheet_batch_update_records,
    dbsheet_batch_delete_records,
    dbsheet_create_sheet,
    dbsheet_update_sheet,
    dbsheet_delete_sheet,
    dbsheet_create_view,
    dbsheet_get_form_meta,
    dbsheet_update_form_meta,
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
    return d if d is not None else resp


def _sheet_id(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        _err("sheet_id 必须为数字")


def _record_fields(rec):
    """从 record 解析出 fields 字典（字段名为 key）。"""
    raw = rec.get("fields")
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if isinstance(raw, str) else {}
    except json.JSONDecodeError:
        return {}


def _apply_client_filter(filter_obj, records):
    """
    在本地按 filter 条件过滤记录。filter_obj: { "mode": "AND"|"OR", "criteria": [ { "field", "op", "values" } ] }。
    支持 op: Equals / Is / Contains；field 为字段名（与记录中 key 一致，如「优先级」）。
    """
    if not records or not isinstance(filter_obj, dict):
        return records
    criteria = filter_obj.get("criteria") or []
    if not criteria:
        return records
    mode = (filter_obj.get("mode") or "AND").upper()
    op_key = "op" if "op" in (criteria[0] or {}) else "operator"

    def match_one(rec):
        obj = _record_fields(rec)
        results = []
        for c in criteria:
            if not isinstance(c, dict):
                results.append(False)
                continue
            field = c.get("field") or c.get("field_id")
            vals = c.get("values")
            if vals is None and "value" in c:
                vals = [c["value"]]
            if vals is None:
                vals = []
            op = (c.get(op_key) or c.get("operator") or "").strip()
            raw_val = obj.get(field) if field else None
            # 单选/多选返回可能是 list，如 ["医疗"]；统一按“可比较值”处理
            if isinstance(raw_val, list):
                compare_val = raw_val[0] if len(raw_val) > 0 else None
            else:
                compare_val = raw_val
            if op in ("Equals", "Is", "eq", "is"):
                results.append(compare_val in vals if vals else (compare_val is None or compare_val == ""))
            elif op in ("Contains", "contains"):
                if compare_val is None:
                    results.append(False)
                else:
                    s = str(compare_val)
                    results.append(any(str(v) in s for v in vals))
            elif op in ("Empty", "empty"):
                results.append(compare_val is None or compare_val == "" or (isinstance(raw_val, list) and len(raw_val) == 0))
            elif op in ("NotEmpty", "NotEmpty", "not_empty"):
                results.append(compare_val not in (None, "") and (not isinstance(raw_val, list) or len(raw_val) > 0))
            else:
                results.append(True)
        return all(results) if mode == "AND" else any(results)

    return [r for r in records if match_one(r)]


def cmd_schema(args):
    file_id = (args.file_id or "").strip()
    if not file_id:
        _err("请提供 file_id")
    resp = dbsheet_get_schema(file_id=file_id)
    data = _check_resp(resp)
    sheets = data.get("sheets", []) if isinstance(data, dict) else []
    md = ["## 多维表 Schema", "", f"- **file_id**：`{file_id}`", f"- **数据表数**：{len(sheets)}", ""]
    for s in sheets[:20]:
        views = s.get("views") or []
        fields = s.get("fields") or []
        md.append(f"- **{s.get('name', '-')}** `sheet_id={s.get('id', '')}` — 视图 {len(views)} 个，字段 {len(fields)} 个，记录数 {s.get('records_count', '-')}")
    _out(md, data)


def cmd_list_records(args):
    file_id = (getattr(args, "file_id", None) or "").strip()
    sheet_id = _sheet_id(getattr(args, "sheet_id", 0))
    if not file_id:
        _err("请提供 file_id 与 sheet_id")
    kwargs = {}
    if getattr(args, "page_size", None) is not None:
        kwargs["page_size"] = args.page_size
    if getattr(args, "page_token", None):
        kwargs["page_token"] = args.page_token
    if getattr(args, "max_records", None) is not None:
        kwargs["max_records"] = args.max_records
    if getattr(args, "view_id", None):
        kwargs["view_id"] = args.view_id
    if getattr(args, "fields", None):
        kwargs["fields"] = [x.strip() for x in args.fields.split(",") if x.strip()]
    if getattr(args, "text_value", None):
        kwargs["text_value"] = args.text_value
    filter_applied = False
    filter_obj = None
    if getattr(args, "filter", None):
        raw = (args.filter or "").strip()
        if raw:
            try:
                filter_obj = json.loads(raw)
                # API 原生支持 filter：body.filter 为 { mode?: "AND"|"OR", criteria: [{ field, operator, values }] }
                if isinstance(filter_obj, dict) and "criteria" in filter_obj:
                    for c in filter_obj.get("criteria") or []:
                        if isinstance(c, dict) and "op" in c and "operator" not in c:
                            c["operator"] = c.pop("op")
                kwargs["filter_body"] = filter_obj
                filter_applied = True
            except json.JSONDecodeError as e:
                _err("--filter 必须是合法 JSON，如 {\"mode\":\"AND\",\"criteria\":[{\"field\":\"字段名\",\"operator\":\"Equals\",\"values\":[\"值\"]}]}，operator 可选 Equals/Contains/Empty/NotEmpty 等：" + str(e))
    resp = dbsheet_list_records(file_id=file_id, sheet_id=sheet_id, **kwargs)
    data = _check_resp(resp)
    records = data.get("records", []) if isinstance(data, dict) else []
    page_token = (data or {}).get("page_token", "")
    client_filtered = False
    if filter_applied and filter_obj:
        orig_count = len(records)
        records = _apply_client_filter(filter_obj, records)
        client_filtered = len(records) != orig_count or orig_count == 0
        if isinstance(data, dict):
            data["records"] = records
    md = [
        "## 记录列表",
        "",
        f"- **file_id**：`{file_id}` **sheet_id**：`{sheet_id}`",
        f"- **本页**：{len(records)} 条" + ("（已按 --filter 客户端筛选）" if client_filtered and filter_applied else ""),
    ]
    if filter_applied:
        md.append("- **筛选**：`--filter` 已传入" + ("，已做客户端筛选" if client_filtered else "（API 未生效时可在本地筛选）"))
    if page_token:
        md.append(f"- **下一页**：`--page-token {page_token}`")
    md.append("")
    for i, rec in enumerate(records[:30], 1):
        rid = rec.get("id", "")
        fields_preview = (rec.get("fields") or "")[:80]
        md.append(f"{i}. `{rid}` — {fields_preview}...")
    _out(md, data)


def cmd_get_record(args):
    file_id = (getattr(args, "file_id", None) or "").strip()
    sheet_id = _sheet_id(getattr(args, "sheet_id", 0))
    record_id = (getattr(args, "record_id", None) or "").strip()
    if not file_id or not record_id:
        _err("请提供 file_id、sheet_id、record_id")
    resp = dbsheet_get_record(file_id=file_id, sheet_id=sheet_id, record_id=record_id)
    data = _check_resp(resp)
    rec = (data or {}).get("record") if isinstance(data, dict) else {}
    md = ["## 记录详情", "", f"- **record_id**：`{record_id}`", ""]
    if rec:
        md.append("**fields**：" + (rec.get("fields") or "")[:200])
    _out(md, data)


def cmd_search_records(args):
    file_id = (getattr(args, "file_id", None) or "").strip()
    sheet_id = _sheet_id(getattr(args, "sheet_id", 0))
    ids = getattr(args, "record_ids", None) or []
    if not file_id or not ids:
        _err("请提供 file_id、sheet_id 及至少一个 record_id")
    resp = dbsheet_search_records(file_id=file_id, sheet_id=sheet_id, record_ids=ids)
    data = _check_resp(resp)
    records = data.get("records", []) if isinstance(data, dict) else []
    md = ["## 检索多条记录", "", f"- **共 {len(records)} 条**", ""]
    for i, rec in enumerate(records[:20], 1):
        md.append(f"{i}. `{rec.get('id', '')}` — {(rec.get('fields') or '')[:60]}...")
    _out(md, data)


def cmd_create_records(args):
    file_id = (getattr(args, "file_id", None) or "").strip()
    sheet_id = _sheet_id(getattr(args, "sheet_id", 0))
    if not file_id:
        _err("请提供 file_id 与 sheet_id")
    raw = getattr(args, "json", None)
    if not raw:
        _err("请通过 --json 传入 records 数组（每项为 { \"fields_value\": \"{\\\"字段名\\\": 值}\" } 或直接为字段对象）")
    try:
        records = json.loads(raw)
        if not isinstance(records, list):
            records = [records]
    except json.JSONDecodeError as e:
        _err("--json 必须是合法 JSON：" + str(e))
    resp = dbsheet_batch_create_records(file_id=file_id, sheet_id=sheet_id, records=records)
    data = _check_resp(resp)
    created = (data or {}).get("records", []) if isinstance(data, dict) else []
    md = ["## 已创建记录", "", f"- **共 {len(created)} 条**", ""]
    for r in created[:20]:
        md.append(f"- `{r.get('id', '')}`")
    _out(md, data)


def cmd_update_records(args):
    file_id = (getattr(args, "file_id", None) or "").strip()
    sheet_id = _sheet_id(getattr(args, "sheet_id", 0))
    if not file_id:
        _err("请提供 file_id 与 sheet_id")
    raw = getattr(args, "json", None)
    if not raw:
        _err("请通过 --json 传入 records 数组（每项含 id、fields_value）")
    try:
        records = json.loads(raw)
        if not isinstance(records, list):
            records = [records]
    except json.JSONDecodeError as e:
        _err("--json 必须是合法 JSON：" + str(e))
    resp = dbsheet_batch_update_records(file_id=file_id, sheet_id=sheet_id, records=records)
    data = _check_resp(resp)
    updated = (data or {}).get("records", []) if isinstance(data, dict) else []
    md = ["## 已更新记录", "", f"- **共 {len(updated)} 条**", ""]
    for r in updated[:20]:
        md.append(f"- `{r.get('id', '')}`")
    _out(md, data)


def cmd_delete_records(args):
    file_id = (getattr(args, "file_id", None) or "").strip()
    sheet_id = _sheet_id(getattr(args, "sheet_id", 0))
    record_ids = getattr(args, "record_ids", None) or []
    if not file_id or not record_ids:
        _err("请提供 file_id、sheet_id 及至少一个 record_id")
    resp = dbsheet_batch_delete_records(file_id=file_id, sheet_id=sheet_id, record_ids=record_ids)
    data = _check_resp(resp)
    results = (data or {}).get("records", []) if isinstance(data, dict) else []
    md = ["## 已删除记录", "", f"- **请求删除 {len(record_ids)} 条**", ""]
    for r in results[:20]:
        md.append(f"- `{r.get('id', '')}` deleted={r.get('deleted', False)}")
    _out(md, data)


def cmd_delete_empty_records(args):
    """
    删除表中所有「空记录」（fields 为 {} 或空）。
    新建 .dbt 多维表时服务端会预置多行空记录，新记录通过 API 只能追加到末尾；
    若希望新插入记录显示在第 1 行，可先执行本命令再 create-records。
    """
    file_id = (getattr(args, "file_id", None) or "").strip()
    sheet_id = _sheet_id(getattr(args, "sheet_id", 0))
    if not file_id:
        _err("请提供 file_id 与 sheet_id")
    empty_ids = []
    page_token = None
    while True:
        kw = {"page_size": 100}
        if page_token:
            kw["page_token"] = page_token
        resp = dbsheet_list_records(file_id=file_id, sheet_id=sheet_id, **kw)
        data = _check_resp(resp)
        records = data.get("records", []) if isinstance(data, dict) else []
        for rec in records:
            fs = rec.get("fields")
            if fs is None or fs == "" or fs == "{}":
                empty_ids.append(rec.get("id"))
            elif isinstance(fs, str):
                try:
                    if not json.loads(fs):
                        empty_ids.append(rec.get("id"))
                except json.JSONDecodeError:
                    pass
        page_token = (data or {}).get("page_token") or ""
        if not page_token:
            break
    if not empty_ids:
        _out(["## 空记录清理", "", "未发现空记录，无需删除。"], {"deleted_count": 0, "records": []})
        return
    deleted_count = 0
    batch_size = 50
    for i in range(0, len(empty_ids), batch_size):
        chunk = empty_ids[i : i + batch_size]
        resp = dbsheet_batch_delete_records(file_id=file_id, sheet_id=sheet_id, record_ids=chunk)
        _check_resp(resp)
        deleted_count += len(chunk)
    md = [
        "## 已删除空记录",
        "",
        f"- **共删除 {deleted_count} 条**（fields 为空的记录）",
        "",
        "新建 .dbt 时服务端会预置多行空记录，新记录会追加在末尾；删除空记录后，再插入的记录将显示在表前列。",
    ]
    _out(md, {"deleted_count": deleted_count, "record_ids": empty_ids})


def cmd_create_sheet(args):
    file_id = (getattr(args, "file_id", None) or "").strip()
    if not file_id:
        _err("请提供 file_id")
    raw = getattr(args, "json", None)
    if not raw:
        _err("请通过 --json 传入请求体，如 {\"name\":\"表名\",\"fields\":[{\"name\":\"标题\",\"field_type\":\"MultiLineText\"}],\"views\":[{\"name\":\"表格\",\"view_type\":\"Grid\"}]}")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        _err("--json 必须是合法 JSON：" + str(e))
    resp = dbsheet_create_sheet(
        file_id=file_id,
        name=body.get("name"),
        fields=body.get("fields"),
        views=body.get("views"),
        position=body.get("position"),
    )
    data = _check_resp(resp)
    sheet = (data or {}).get("sheet") if isinstance(data, dict) else {}
    md = ["## 已创建数据表", "", f"- **sheet_id**：`{sheet.get('id', '-')}`", f"- **名称**：{sheet.get('name', '-')}", ""]
    _out(md, data)


def cmd_update_sheet(args):
    file_id = (getattr(args, "file_id", None) or "").strip()
    sheet_id = _sheet_id(getattr(args, "sheet_id", 0))
    name = getattr(args, "name", None)
    if not file_id or name is None:
        _err("请提供 file_id、sheet_id 与 --name（新表名）")
    resp = dbsheet_update_sheet(file_id=file_id, sheet_id=sheet_id, name=name)
    data = _check_resp(resp)
    _out(["## 已更新数据表", "", f"表 `{sheet_id}` 名称已更新为：**{name}**"], data)


def cmd_delete_sheet(args):
    file_id = (getattr(args, "file_id", None) or "").strip()
    sheet_id = _sheet_id(getattr(args, "sheet_id", 0))
    if not file_id:
        _err("请提供 file_id 与 sheet_id")
    resp = dbsheet_delete_sheet(file_id=file_id, sheet_id=sheet_id)
    data = _check_resp(resp)
    _out(["## 已删除数据表", "", f"表 `{sheet_id}` 已删除"], data)


def cmd_create_view(args):
    file_id = (getattr(args, "file_id", None) or "").strip()
    sheet_id = _sheet_id(getattr(args, "sheet_id", 0))
    name = getattr(args, "name", None)
    view_type = getattr(args, "view_type", None) or "Grid"
    if not file_id:
        _err("请提供 file_id、sheet_id，可选 --name、--view-type（默认 Grid）")
    resp = dbsheet_create_view(file_id=file_id, sheet_id=sheet_id, name=name, view_type=view_type)
    data = _check_resp(resp)
    view = (data or {}).get("view") if isinstance(data, dict) else {}
    md = ["## 已创建视图", "", f"- **view_id**：`{view.get('id', '-')}`", f"- **名称**：{view.get('name', '-')}", f"- **类型**：{view.get('view_type', '-')}", ""]
    _out(md, data)


def cmd_get_form_meta(args):
    file_id = (getattr(args, "file_id", None) or "").strip()
    sheet_id = _sheet_id(getattr(args, "sheet_id", 0))
    view_id = (getattr(args, "view_id", None) or "").strip()
    if not file_id or not view_id:
        _err("请提供 file_id、sheet_id、view_id（表单视图 ID，从 schema 的 views 中获取）")
    resp = dbsheet_get_form_meta(file_id=file_id, sheet_id=sheet_id, view_id=view_id)
    data = _check_resp(resp)
    md = ["## 表单视图元数据", "", f"- **名称**：{data.get('name', '-')}", f"- **描述**：{data.get('description', '-')}", ""]
    _out(md, data)


def cmd_update_form_meta(args):
    file_id = (getattr(args, "file_id", None) or "").strip()
    sheet_id = _sheet_id(getattr(args, "sheet_id", 0))
    view_id = (getattr(args, "view_id", None) or "").strip()
    name = getattr(args, "name", None)
    description = getattr(args, "description", None)
    if not file_id or not view_id:
        _err("请提供 file_id、sheet_id、view_id")
    if name is None and description is None:
        _err("请至少提供 --name 或 --description")
    resp = dbsheet_update_form_meta(file_id=file_id, sheet_id=sheet_id, view_id=view_id, name=name, description=description)
    data = _check_resp(resp)
    _out(["## 已更新表单视图元数据", ""], data)


def main():
    parser = argparse.ArgumentParser(description="多维表（DbSheet）：Schema、列举/检索/增删改记录")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("schema", help="获取多维表 Schema")
    p.add_argument("file_id", help="多维表文件 ID")
    p.set_defaults(func=cmd_schema)

    p = sub.add_parser("list-records", help="列举记录")
    p.add_argument("file_id", help="多维表文件 ID")
    p.add_argument("sheet_id", help="数据表 ID（数字）")
    p.add_argument("--page-size", type=int, default=None)
    p.add_argument("--page-token", default=None)
    p.add_argument("--max-records", type=int, default=None)
    p.add_argument("--view-id", default=None)
    p.add_argument("--fields", default=None, help="逗号分隔字段名或 id")
    p.add_argument("--text-value", default=None, choices=("original", "text", "compound"))
    p.add_argument("--filter", default=None, help='筛选条件 JSON（API 原生支持）。格式: {"mode":"AND"|"OR","criteria":[{"field":"字段名或id","operator":"Equals","values":["值"]}]}，operator 可选 Equals/NotEqu/Contains/Empty/NotEmpty/Greater/Less 等')
    p.set_defaults(func=cmd_list_records)

    p = sub.add_parser("get-record", help="检索单条记录")
    p.add_argument("file_id")
    p.add_argument("sheet_id")
    p.add_argument("record_id")
    p.set_defaults(func=cmd_get_record)

    p = sub.add_parser("search-records", help="检索多条记录")
    p.add_argument("file_id")
    p.add_argument("sheet_id")
    p.add_argument("record_ids", nargs="+", help="记录 ID 列表")
    p.set_defaults(func=cmd_search_records)

    p = sub.add_parser("create-records", help="批量创建记录")
    p.add_argument("file_id")
    p.add_argument("sheet_id")
    p.add_argument("--json", required=True, help='records 数组 JSON，每项含 fields_value 或为字段对象')
    p.set_defaults(func=cmd_create_records)

    p = sub.add_parser("update-records", help="批量更新记录")
    p.add_argument("file_id")
    p.add_argument("sheet_id")
    p.add_argument("--json", required=True, help='records 数组 JSON，每项含 id、fields_value')
    p.set_defaults(func=cmd_update_records)

    p = sub.add_parser("delete-records", help="批量删除记录")
    p.add_argument("file_id")
    p.add_argument("sheet_id")
    p.add_argument("record_ids", nargs="+", help="记录 ID 列表")
    p.set_defaults(func=cmd_delete_records)

    p = sub.add_parser("delete-empty-records", help="删除表中所有空记录（fields 为空），便于新插入记录显示在表前列")
    p.add_argument("file_id")
    p.add_argument("sheet_id")
    p.set_defaults(func=cmd_delete_empty_records)

    p = sub.add_parser("create-sheet", help="创建数据表")
    p.add_argument("file_id", help="多维表文件 ID")
    p.add_argument("--json", required=True, help='请求体 JSON：name?, fields, views?, position?')
    p.set_defaults(func=cmd_create_sheet)

    p = sub.add_parser("update-sheet", help="更新数据表（如改表名）")
    p.add_argument("file_id")
    p.add_argument("sheet_id")
    p.add_argument("--name", required=True, help="新表名")
    p.set_defaults(func=cmd_update_sheet)

    p = sub.add_parser("delete-sheet", help="删除数据表")
    p.add_argument("file_id")
    p.add_argument("sheet_id")
    p.set_defaults(func=cmd_delete_sheet)

    p = sub.add_parser("create-view", help="创建视图")
    p.add_argument("file_id")
    p.add_argument("sheet_id")
    p.add_argument("--name", default=None, help="视图名称")
    p.add_argument("--view-type", default="Grid", choices=("Grid", "Kanban", "Gallery", "Form", "Gantt", "Query"), help="视图类型，默认 Grid")
    p.set_defaults(func=cmd_create_view)

    p = sub.add_parser("get-form-meta", help="获取表单视图元数据")
    p.add_argument("file_id")
    p.add_argument("sheet_id")
    p.add_argument("view_id", help="表单视图 ID（从 schema 的 views 获取）")
    p.set_defaults(func=cmd_get_form_meta)

    p = sub.add_parser("update-form-meta", help="更新表单视图元数据")
    p.add_argument("file_id")
    p.add_argument("sheet_id")
    p.add_argument("view_id")
    p.add_argument("--name", default=None, help="表单名称")
    p.add_argument("--description", default=None, help="表单描述")
    p.set_defaults(func=cmd_update_form_meta)

    args = parser.parse_args()
    try:
        args.func(args)
    except ValueError as e:
        _err(str(e))
    except Exception as e:
        _err("请求失败: " + str(e))


if __name__ == "__main__":
    main()
