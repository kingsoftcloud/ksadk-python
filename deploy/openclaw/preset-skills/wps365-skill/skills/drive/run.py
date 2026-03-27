#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云文档 Drive：调用 V7 Drive 接口，实现文件上传、列表、详情等功能。
需在 wps365-skill 根目录执行，并设置环境变量 wps_sid。
用法:
  python skills/drive/run.py upload <文件路径>
  python skills/drive/run.py list
  python skills/drive/run.py get <file_id>
  python skills/drive/run.py extract <file_id|link_id> [--format plain|markdown]
  python skills/drive/run.py read <file_id|link_id>   # extract 的别名
"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from wpsv7client import (
    upload_simple,
    update_file,
    list_files,
    get_file,
    get_file_directly,
    get_link_meta,
    get_file_download_url,
    get_file_content_extract,
    get_drive_id,
    create_file,
    create_otl_document,
    write_airpage_content,
    search_files,
    convert_file,
    list_latest_items,
    list_star_items,
    batch_create_star_items,
    batch_delete_star_items,
    list_drive_labels,
    get_drive_label_meta,
    list_drive_label_objects,
    create_drive_label,
    batch_add_drive_label_objects,
    batch_remove_drive_label_objects,
    list_deleted_files,
    restore_deleted_file,
    move_file,
    copy_file,
    rename_file,
    save_as_file,
    check_name_exists,
    open_file_link,
    close_file_link,
    WpsV7Client,
)


def _out(md_lines, data=None):
    """输出 Markdown 摘要；data 非 None 时追加「原始数据 (JSON)」块。"""
    lines = [""] + md_lines
    if data is not None:
        lines += ["", "## 原始数据 (JSON)", "", "```json", json.dumps(data, ensure_ascii=False, indent=2), "```"]
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


def _get_drive_id(drive_name):
    """将drive名称转换为实际的drive ID"""
    if drive_name in ["private", "roaming", "special"]:
        # 获取实际的drive ID
        c = WpsV7Client()
        resp = c.get("/v7/drives?allotee_type=user&page_size=10")
        if resp.get("code") != 0:
            _err(f"获取云盘列表失败: {resp.get('msg')}")
        
        items = resp.get("data", {}).get("items", [])
        for item in items:
            if drive_name == "private" and item.get("name") == "我的企业文档":
                return item.get("id")
            elif drive_name == "roaming" and item.get("name") == "自动备份":
                return item.get("id")
            elif drive_name == "special" and item.get("source") == "special":
                return item.get("id")
        
        # 如果没有找到，返回第一个drive
        if items:
            return items[0].get("id")
        
        _err(f"未找到云盘: {drive_name}")
    else:
        # 已经是实际的drive ID
        return drive_name


def _get_file_type(drive_id, file_id):
    """根据文件名扩展判断类型（用于 extract/read/write 展示）。优先用 get_file_directly。"""
    resp = get_file_directly(file_id)
    if resp.get("code") != 0 or not resp.get("data"):
        resp = get_file(drive_id=drive_id, file_id=file_id)
    data = _check_resp(resp)
    name = (data.get("name") or "").lower()
    if name.endswith(".otl"):
        return "ap"
    if name.endswith(".dbt"):
        return "dbsheet"
    if name.endswith((".docx", ".doc", ".wps")):
        return "doc"
    if name.endswith(".pdf"):
        return "pdf"
    if name.endswith((".pptx", ".ppt", ".wpp")):
        return "ppt"
    if name.endswith((".xlsx", ".xls", ".et")):
        return "sheet"
    return "unknown"


def _read_local_file(file_path):
    """读取本地文件内容（用于 write --file）。"""
    p = Path(file_path)
    if not p.exists():
        _err(f"文件不存在: {file_path}")
    return p.read_text(encoding="utf-8")


def _resolve_file_and_drive(id_or_link_id, default_drive="private"):
    """
    将 file_id 或 link_id 解析为 (file_id, drive_id)。
    若传入的是 link_id，则调用 GET /v7/links/{link_id}/meta 取 file_id/drive_id；否则视为 file_id，drive 用默认值。
    """
    raw = (id_or_link_id or "").strip()
    if not raw:
        return None, None
    resp = get_link_meta(link_id=raw)
    if resp.get("code") == 0 and resp.get("data"):
        data = resp["data"]
        if data.get("file_id"):
            did = data.get("drive_id")
            if did is None or did == "":
                did = _get_drive_id(default_drive)
            else:
                did = str(did)
            return data["file_id"], did
    # 视为 file_id
    return raw, _get_drive_id(default_drive)


def _is_single_md_file(file_path):
    """判断是否为单个 .md 文件（不区分大小写）。"""
    p = Path(file_path)
    return p.is_file() and p.suffix.lower() == ".md"


def _parent_path_list(s):
    """将 '我的文档' 或 '我的文档/子目录' 转为 ['我的文档'] 或 ['我的文档','子目录']。"""
    if not (s or "").strip():
        return ["我的文档"]
    return [p.strip() for p in s.strip().split("/") if p.strip()] or ["我的文档"]


def _upload_md_as_airpage(file_path, args):
    """将 .md 文件上传为智能文档（.otl）：创建文档并写入内容（365 内容 API）。"""
    p = Path(file_path)
    content = p.read_text(encoding="utf-8", errors="replace")
    title = (getattr(args, "filename", None) or "").strip() or (p.stem or "文档")
    drive_id = _get_drive_id(args.drive or "private")
    parent_path = _parent_path_list(args.path or "我的文档")

    create_resp = create_otl_document(
        drive_id=drive_id,
        file_name=title,
        parent_path=parent_path,
        on_name_conflict="rename",
    )
    data = _check_resp(create_resp)
    file_id = (data or {}).get("id")
    if not file_id:
        _err("创建智能文档失败：未返回文件 id")

    write_airpage_content(file_id, title, content, pos="begin")
    link_url = (data or {}).get("link_url") or ""
    link_id = (data or {}).get("link_id") or ""

    md = [
        "## 已上传为智能文档",
        "",
        f"已将 Markdown 写入智能文档 **{title}**。",
        "",
        f"- **文件 ID**：`{file_id}`",
        f"- **链接**：{link_url}",
    ]
    if link_id:
        md.append("")
        md.append("发送云文档消息所需信息：")
        md.append("```json")
        md.append(f'{{"type": "cloud", "cloud": {{"id": "{link_id}", "link_url": "{link_url}", "link_id": "{link_id}"}}}}')
        md.append("```")
    _out(md, {"success": True, "file_id": file_id, "title": title, "link_url": link_url, "link_id": link_id})


def cmd_upload(args):
    if not args.file_path:
        _err("请指定文件路径，例如: run.py upload /path/to/file.md")
    
    file_path = args.file_path
    if not os.path.isabs(file_path):
        file_path = os.path.join(os.getcwd(), file_path)
    
    if not os.path.exists(file_path):
        _err(f"文件不存在: {file_path}")
    
    # 单文件为 .md 时，上传为智能文档（.otl）
    if _is_single_md_file(file_path):
        _upload_md_as_airpage(file_path, args)
        return
    
    try:
        # 获取实际的drive ID
        drive_id = _get_drive_id(args.drive or "private")
        
        data = upload_simple(
            file_path=file_path,
            drive_id=drive_id,
            parent_id=args.parent or "root",
            parent_path=args.path.split("/") if args.path else None,
            file_name=getattr(args, "filename", None),
        )
    except Exception as e:
        _err(f"上传失败: {str(e)}")
    
    # 处理响应格式：文件信息可能在data.data中，或在data.file中
    if "data" in data:
        # upload_simple返回完整响应，文件信息在data.data中
        file_data = data.get("data", {})
        if "file" in file_data:
            file_info = file_data.get("file", {})
        else:
            file_info = file_data
    elif "file" in data:
        file_info = data.get("file", {})
    else:
        file_info = data
    
    md = ["## 文件上传成功", "", f"文件已上传至云端：", "",
          f"- **文件名**: {file_info.get('name', '-')}",
          f"- **文件ID**: `{file_info.get('id', '-')}`",
          f"- **链接**: {file_info.get('link_url', '-')}",
          f"- **大小**: {file_info.get('size', '-')} 字节",
          "",
          f"发送云文档消息所需信息：",
          f"```json",
          f'{{"type": "cloud", "cloud": {{"id": "{file_info.get("id", "")}", "link_url": "{file_info.get("link_url", "")}", "link_id": "{file_info.get("link_id", "")}"}}}}',
          f"```"]
    _out(md, data)


def cmd_update(args):
    """更新现有云文档文件（上传新版本覆盖）"""
    if not args.file_path:
        _err("请指定文件路径，例如: run.py update <file_id> /path/to/file.docx")
    
    file_path = args.file_path
    if not os.path.isabs(file_path):
        file_path = os.path.join(os.getcwd(), file_path)
    
    if not os.path.exists(file_path):
        _err(f"文件不存在: {file_path}")
    
    # 解析 file_id（支持 link_id）
    file_id, drive_id = _resolve_file_and_drive(args.file_id, args.drive or "private")
    if not file_id:
        _err("无法解析 file_id")
    
    try:
        data = update_file(
            file_id=file_id,
            file_path=file_path,
            drive_id=drive_id,
        )
    except Exception as e:
        _err(f"更新失败: {str(e)}")
    
    # 处理响应格式
    if "data" in data:
        file_info = data.get("data", {})
    else:
        file_info = data
    
    md = [
        "## 文件更新成功", 
        "", 
        f"文件已更新为新版本：", 
        "",
        f"- **文件名**: {file_info.get('name', '-')}",
        f"- **文件ID**: `{file_info.get('id', '-')}`",
        f"- **版本**: {file_info.get('version', '-')}",
        f"- **链接**: {file_info.get('link_url', '-')}",
        f"- **大小**: {file_info.get('size', '-')} 字节",
        f"- **修改时间**: {file_info.get('mtime', '-')}",
    ]
    _out(md, data)


def cmd_write(args):
    """将 Markdown 内容写入文档：智能文档用 insertContent，文字/PDF 用转换+覆盖（复用 update_file）。"""
    file_id, drive_id = _resolve_file_and_drive(
        args.file_id, getattr(args, "drive", None) or "private"
    )
    if not file_id:
        _err("无法解析 file_id")

    content = getattr(args, "content", None)
    if getattr(args, "file", None):
        content = _read_local_file(args.file)
    if not content:
        _err("请通过 --content 或 --file 指定要写入的内容")

    file_resp = get_file_directly(file_id, with_drive=True)
    if file_resp.get("code") == 0 and file_resp.get("data"):
        file_data = file_resp.get("data", {})
        drive_id = file_data.get("drive_id") or drive_id
    else:
        file_resp = get_file(drive_id=drive_id, file_id=file_id)
        file_data = file_resp.get("data") or {}
    file_name = file_data.get("name", "-")
    file_type = _get_file_type(drive_id, file_id)

    if file_type in ("ap", "unknown"):
        pos = "end" if getattr(args, "mode", "overwrite") == "append" else "begin"
        title = getattr(args, "title", None) or file_name.replace(".otl", "")
        resp = write_airpage_content(
            file_id=file_id,
            title=title,
            content=content,
            pos=pos,
        )
        success = resp.get("code") == 0 or resp.get("result") == "ok" or not resp.get("error")
        if getattr(args, "json", False):
            out = {"success": success, "file_id": file_id, "file_name": file_name, "pos": pos, "content_length": len(content)}
            if not success:
                out["message"] = resp.get("msg") or resp.get("error") or "未知错误"
            print(json.dumps(out, ensure_ascii=False, indent=2))
            sys.stdout.flush()
            return
        md = [
            "## Markdown 写入（智能文档）",
            "",
            f"- **文件名**: {file_name}",
            f"- **文件ID**: `{file_id}`",
            f"- **写入位置**: {pos}",
            f"- **内容长度**: {len(content)} 字符",
            f"- **状态**: {'✅ 成功' if success else '❌ 失败'}",
        ]
        if not success:
            md.append("")
            md.append(f"错误信息: {resp.get('msg') or resp.get('error') or '未知错误'}")
        _out(md, resp)

    elif file_type in ("doc", "pdf"):
        target_format = "pdf" if file_type == "pdf" else "docx"
        temp_md_file = None
        temp_target_file = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
                f.write(content)
                temp_md_file = f.name
            convert_resp = convert_file(
                source_file_path=temp_md_file,
                target_format=target_format,
                template_file_path=getattr(args, "template", None) if target_format == "docx" else None,
            )
            if convert_resp.get("code") != 0:
                _err(f"Markdown 转换失败: {convert_resp.get('msg', '未知错误')}")
            target_content = convert_resp.get("data")
            if not target_content:
                _err("转换结果为空")
            with tempfile.NamedTemporaryFile(mode="wb", suffix=f".{target_format}", delete=False) as f:
                f.write(target_content)
                temp_target_file = f.name
            update_resp = update_file(
                file_id=file_id,
                file_path=temp_target_file,
                drive_id=drive_id,
            )
            update_data = update_resp.get("data") or {}
            success = update_resp.get("code") == 0
            type_name = "PDF文档" if file_type == "pdf" else "文字文档"
            if getattr(args, "json", False):
                out = {"success": success, "file_id": file_id, "file_name": file_name, "version": update_data.get("version"), "content_length": len(content)}
                if not success:
                    out["message"] = update_resp.get("msg") or "未知错误"
                print(json.dumps(out, ensure_ascii=False, indent=2))
                sys.stdout.flush()
                return
            md = [
                f"## Markdown 写入（{type_name}）",
                "",
                f"- **文件名**: {file_name}",
                f"- **文件ID**: `{file_id}`",
                f"- **转换大小**: {len(target_content)} 字节",
                f"- **新版本**: {update_data.get('version', '-')}",
                f"- **内容长度**: {len(content)} 字符",
                f"- **状态**: {'✅ 成功' if success else '❌ 失败'}",
            ]
            if not success:
                md.append("")
                md.append(f"错误信息: {update_resp.get('msg') or '未知错误'}")
            _out(md, update_resp)
        finally:
            if temp_md_file and os.path.exists(temp_md_file):
                os.unlink(temp_md_file)
            if temp_target_file and os.path.exists(temp_target_file):
                os.unlink(temp_target_file)
    else:
        _err(f"不支持的文件类型: {file_type}（当前仅支持智能文档 .otl、文字文档 .docx 和 PDF文档 .pdf）")


def cmd_list(args):
    # 获取实际的drive ID
    drive_id = _get_drive_id(args.drive or "private")

    all_items = []
    page_token = (getattr(args, "page_token", None) or "").strip() or None
    last_data = {}

    while True:
        resp = list_files(
            drive_id=drive_id,
            parent_id=args.parent or "root",
            page_size=args.page_size or 50,
            page_token=page_token,
        )
        data = _check_resp(resp)
        last_data = data or {}

        items = (data or {}).get("items") or []
        all_items.extend(items)

        next_token = (data or {}).get("next_page_token") or ""
        if not getattr(args, "all", False) or not next_token:
            page_token = next_token or ""
            break
        page_token = next_token

    md = ["## 文件列表", "", f"当前目录共有 **{len(all_items)}** 个文件/文件夹。"]
    if not all_items:
        md.append("")
        md.append("目录为空。")
    else:
        md.append("")
        for i, item in enumerate(all_items, 1):
            item_type = "📁 文件夹" if item.get("type") == "folder" else "📄 文件"
            name = item.get("name", "-")
            file_id = item.get("id", "-")
            size = item.get("size", "-")
            md.append(f"{i}. {item_type} **{name}**")
            md.append(f"   > ID: `{file_id}` | 大小: {size} 字节")

    if (last_data or {}).get("next_page_token") and not getattr(args, "all", False):
        md.append("")
        md.append(f"> 还有更多条目，可使用 `list --page-token {last_data.get('next_page_token')}` 获取下一页，或使用 `list --all` 拉取全部。")

    out_data = dict(last_data or {})
    out_data["items"] = all_items
    _out(md, out_data)


def cmd_get(args):
    if not args.file_id:
        _err("请指定文件ID或 link_id，例如: run.py get <file_id|link_id>")
    file_id, drive_id = _resolve_file_and_drive(args.file_id, args.drive or "private")
    if not file_id:
        _err("无法解析 file_id")
    resp = get_file_directly(file_id, with_link=True, with_drive=True)
    if resp.get("code") != 0 or not resp.get("data"):
        resp = get_file(drive_id=drive_id, file_id=file_id)
    data = _check_resp(resp)
    md = ["## 文件详情", "",
          f"- **文件名**: {data.get('name', '-')}",
          f"- **文件ID**: `{data.get('id', '-')}`",
          f"- **链接**: {data.get('link_url', '-')}",
          f"- **大小**: {data.get('size', '-')} 字节",
          f"- **类型**: {data.get('type', '-')}"]
    _out(md, data)


def cmd_download(args):
    if not args.file_id:
        _err("请指定文件ID或 link_id，例如: run.py download <file_id|link_id>")
    file_id, drive_id = _resolve_file_and_drive(args.file_id, args.drive or "private")
    if not file_id:
        _err("无法解析 file_id")
    meta = get_file_directly(file_id, with_drive=True)
    if meta.get("code") == 0 and meta.get("data"):
        drive_id = meta.get("data", {}).get("drive_id") or drive_id
    resp = get_file_download_url(
        drive_id=drive_id,
        file_id=file_id,
    )
    data = _check_resp(resp)
    md = ["## 文件下载链接", "",
          f"- **文件名**: {data.get('name', '-')}",
          f"- **下载链接**: {data.get('url', '-')}"]
    _out(md, data)


def cmd_extract(args):
    """按云文档抽取正文（plain/markdown/html），支持 read 别名；GET .../content。"""
    if not getattr(args, "file_id", None):
        _err("请指定文件ID或 link_id，例如: run.py extract <file_id|link_id> 或 run.py read <file_id|link_id>")
    file_id, drive_id = _resolve_file_and_drive(
        args.file_id, getattr(args, "drive", None) or "private"
    )
    if not file_id:
        _err("无法解析 file_id")
    fmt = (getattr(args, "format", None) or "markdown").strip().lower()
    if fmt not in ("plain", "markdown", "html", "kdc"):
        fmt = "markdown"
    # 文件信息（用于展示与 --json 输出）；优先用 get_file_directly
    file_resp = get_file_directly(file_id, with_drive=True)
    if file_resp.get("code") == 0 and file_resp.get("data"):
        file_data = file_resp.get("data", {})
        drive_id = file_data.get("drive_id") or drive_id
    else:
        file_resp = get_file(drive_id=drive_id, file_id=file_id)
        file_data = file_resp.get("data") or {}
    file_name = file_data.get("name", "-")
    file_type = _get_file_type(drive_id, file_id) if not getattr(args, "type", None) else getattr(args, "type")

    resp = get_file_content_extract(
        drive_id=drive_id,
        file_id=file_id,
        format=fmt,
    )
    data = _check_resp(resp)
    # 按请求的 format 优先取对应字段，保证默认/--format markdown 时优先输出 markdown
    content = (data.get(fmt) or data.get("markdown") or data.get("plain") or data.get("html") or "").strip()
    out_data = {
        "file_id": file_id,
        "file_name": file_name,
        "file_type": file_type,
        "format": fmt,
        "content": content,
    }

    if getattr(args, "json", False):
        print(json.dumps(out_data, ensure_ascii=False, indent=2))
        sys.stdout.flush()
        return
    if getattr(args, "raw", False):
        print(content)
        sys.stdout.flush()
        return

    md = [
        "## 文档内容抽取",
        "",
        f"- **文件名**: {file_name}",
        f"- **文件ID**: `{file_id}`",
        f"- **类型**: {file_type}",
        f"- **输出格式**: {fmt}",
        "",
        "---",
        "",
    ]
    if content:
        md.append("### 抽取结果")
        md.append("")
        md.append("```" + ("md" if fmt == "markdown" else ""))
        md.append(content)
        md.append("```")
    else:
        md.append("(无正文)")
    _out(md, None)


def cmd_create(args):
    """统一创建能力：新建文件/文件夹/快捷方式。POST /v7/drives/{drive_id}/files/{parent_id}/create。"""
    file_name = (getattr(args, "file_name", None) or "").strip()
    if not file_name:
        _err("请指定名称，例如: run.py create 反馈管理.dbt")

    drive_name = getattr(args, "drive", None) or "private"
    drive_id = get_drive_id(drive_name) if drive_name in ("private", "roaming", "special") else drive_name

    path_value = getattr(args, "path", None)
    if path_value:
        parent_path = _parent_path_list(path_value)
    elif not getattr(args, "parent_id", None):
        # 保持 create 旧行为：未传 parent_id/path 时默认落到“我的文档”。
        parent_path = ["我的文档"]
    else:
        parent_path = None

    file_type = (getattr(args, "file_type", None) or "file").strip().lower()
    on_conflict = getattr(args, "on_conflict", None) or "rename"
    resp = create_file(
        drive_id=drive_id,
        file_name=file_name,
        parent_id=getattr(args, "parent_id", None) or "0",
        file_type=file_type,
        file_id=getattr(args, "file_id", None),
        parent_path=parent_path,
        on_name_conflict=on_conflict,
    )
    data = _check_resp(resp)
    file_id = data.get("id", "-")
    link_id = data.get("link_id", "") or file_id
    link_url = data.get("link_url", "")
    md = [
        "## 已创建对象",
        "",
        f"- **文件名**: {data.get('name', file_name)}",
        f"- **类型**: {file_type}",
        f"- **文件 ID**: `{file_id}`",
        f"- **link_id**: `{link_id}`",
        f"- **链接**: {link_url}",
        "",
        "> 多维表（.dbt）创建后，可使用 dbsheet 技能在该 file_id 下创建数据表与记录。发送云文档消息时 `cloud.id` 使用 **link_id**。",
    ]
    _out(md, data)


def cmd_link_meta(args):
    """根据 link_id 获取分享链接信息（含 file_id），用于 link_id 换 file_id。"""
    if not args.link_id:
        _err("请指定 link_id，例如: run.py link-meta <link_id>")
    resp = get_link_meta(link_id=args.link_id)
    data = _check_resp(resp)
    file_id = data.get("file_id") or "-"
    drive_id = data.get("drive_id") or "-"
    link_url = data.get("url") or data.get("link_url") or "-"
    md = [
        "## 分享链接详情（link_id → file_id）",
        "",
        f"- **link_id**: `{args.link_id}`",
        f"- **file_id**: `{file_id}`",
        f"- **drive_id**: `{drive_id}`",
        f"- **链接**: {link_url}",
    ]
    if file_id != "-":
        md.extend([
            "",
            "> 可使用上述 **file_id** 与 **drive_id** 调用 `get <file_id>` 或 `download <file_id>`（需指定 `--drive <drive_id>`）。",
        ])
    _out(md, data)


def cmd_search(args):
    """搜索云文档文件（简化版，便于 LLM 使用）。GET /v7/files/search。"""
    keyword = (getattr(args, "keyword", None) or "").strip() or None
    scope = getattr(args, "scope", None)
    if scope and isinstance(scope, str):
        scope = [s.strip() for s in scope.split(",") if s.strip()] or None
    resp = search_files(
        keyword=keyword,
        search_type=args.type or "all",
        scope=scope,
        page_size=args.page_size or 20,
        page_token=(getattr(args, "page_token", None) or "").strip() or None,
        with_total=not getattr(args, "no_total", False),
        with_link=True,
    )
    data = _check_resp(resp)
    items = (data or {}).get("items") or []
    total = (data or {}).get("total")
    next_token = (data or {}).get("next_page_token") or ""

    md = ["## 文件搜索结果", ""]
    if keyword:
        md.append(f"关键词：**{keyword}**")
    md.append(f"本页共 **{len(items)}** 条" + (f"，共 **{total}** 条" if total is not None else "") + "。")
    md.append("")
    for i, item in enumerate(items, 1):
        fi = (item.get("file") or item) if isinstance(item, dict) else {}
        name = fi.get("name", "-")
        file_id = fi.get("id", "-")
        size = fi.get("size", "-")
        link_url = fi.get("link_url", "")
        md.append(f"{i}. **{name}**")
        md.append(f"   > ID: `{file_id}` | 大小: {size}" + (f" | [链接]({link_url})" if link_url else ""))
    if next_token:
        md.append("")
        md.append(f"> 更多结果可使用 `search --page-token {next_token}` 获取下一页。")
    _out(md, data if data is not None else resp)

def _split_csv(value):
    if not value or not isinstance(value, str):
        return None
    items = [s.strip() for s in value.split(",") if s.strip()]
    return items or None


_DRIVE_IDS_CACHE = None


def _list_user_drive_ids():
    """获取当前用户可访问的 drive_id 列表（带简单缓存）。"""
    global _DRIVE_IDS_CACHE
    if isinstance(_DRIVE_IDS_CACHE, list) and _DRIVE_IDS_CACHE:
        return _DRIVE_IDS_CACHE
    c = WpsV7Client()
    resp = c.get("/v7/drives?allotee_type=user&page_size=100")
    if resp.get("code") != 0:
        return []
    items = (resp.get("data") or {}).get("items") or []
    _DRIVE_IDS_CACHE = [str(it.get("id")) for it in items if it.get("id")]
    return _DRIVE_IDS_CACHE


def _resolve_file_meta_for_object_id(file_id):
    """将标签对象 ID 解析为文件详情。优先用 GET /v7/files/{file_id}/meta（仅需 file_id）。"""
    if not file_id:
        return None
    resp = get_file_directly(file_id, with_link=True, with_drive=True)
    if resp.get("code") == 0 and resp.get("data"):
        return resp.get("data")
    for drive_id in _list_user_drive_ids():
        resp = get_file(drive_id=drive_id, file_id=file_id)
        if resp.get("code") == 0 and resp.get("data"):
            return resp.get("data")
    return None


def cmd_latest(args):
    """获取最近列表。GET /v7/drive_latest/items。"""
    resp = list_latest_items(
        with_permission=getattr(args, "with_permission", None),
        with_link=getattr(args, "with_link", None),
        page_size=args.page_size or 50,
        page_token=(getattr(args, "page_token", None) or "").strip() or None,
        include_exts=_split_csv(getattr(args, "include_exts", None)),
        exclude_exts=_split_csv(getattr(args, "exclude_exts", None)),
        include_creators=_split_csv(getattr(args, "include_creators", None)),
        exclude_creators=_split_csv(getattr(args, "exclude_creators", None)),
    )
    data = _check_resp(resp)
    items = (data or {}).get("items") or []
    next_token = (data or {}).get("next_page_token") or ""

    md = ["## 最近列表", "", f"本页共 **{len(items)}** 条最近文档。", ""]
    if not items:
        md.append("暂无最近文档。")
    else:
        for i, item in enumerate(items, 1):
            name = item.get("name", "-")
            file_id = item.get("id", "-")
            drive_id = item.get("drive_id", "-")
            link_url = item.get("link_url", "")
            md.append(f"{i}. **{name}**")
            md.append(f"   > file_id: `{file_id}` | drive_id: `{drive_id}`" + (f" | [链接]({link_url})" if link_url else ""))
    if next_token:
        md.append("")
        md.append(f"> 更多结果可使用 `latest --page-token {next_token}` 获取下一页。")
    _out(md, data if data is not None else resp)


def cmd_star(args):
    """获取收藏列表。GET /v7/drive_star/items。"""
    resp = list_star_items(
        with_permission=getattr(args, "with_permission", None),
        with_link=getattr(args, "with_link", None),
        page_size=args.page_size or 50,
        page_token=(getattr(args, "page_token", None) or "").strip() or None,
        order=(getattr(args, "order", None) or "").strip() or None,
        order_by=(getattr(args, "order_by", None) or "").strip() or None,
        include_exts=_split_csv(getattr(args, "include_exts", None)),
        exclude_exts=_split_csv(getattr(args, "exclude_exts", None)),
    )
    data = _check_resp(resp)
    items = (data or {}).get("items") or []
    next_token = (data or {}).get("next_page_token") or ""

    md = ["## 收藏列表", "", f"本页共 **{len(items)}** 条收藏文档。", ""]
    if not items:
        md.append("暂无收藏文档。")
    else:
        for i, item in enumerate(items, 1):
            fi = (item.get("file") or item) if isinstance(item, dict) else {}
            name = fi.get("name", "-")
            file_id = fi.get("id", "-")
            drive_id = fi.get("drive_id", "-")
            link_url = fi.get("link_url", "")
            md.append(f"{i}. **{name}**")
            md.append(f"   > file_id: `{file_id}` | drive_id: `{drive_id}`" + (f" | [链接]({link_url})" if link_url else ""))
    if next_token:
        md.append("")
        md.append(f"> 更多结果可使用 `star --page-token {next_token}` 获取下一页。")
    _out(md, data if data is not None else resp)


def cmd_star_add_items(args):
    """批量添加收藏项。POST /v7/drive_star/items/batch_create"""
    objects = []
    raw_ids = _split_csv(getattr(args, "objects", None))
    if raw_ids:
        objects.extend(raw_ids)

    for field in ("objects_json", "items_json"):
        raw_json = (getattr(args, field, None) or "").strip()
        if raw_json:
            try:
                parsed = json.loads(raw_json)
                if isinstance(parsed, list):
                    objects.extend(parsed)
                else:
                    _err(f"--{field.replace('_', '-')} 必须是 JSON 数组")
            except json.JSONDecodeError:
                _err(f"--{field.replace('_', '-')} 不是合法 JSON")

    if not objects:
        _err("请至少通过 --objects / --objects-json / --items-json 传入一个对象")

    resp = batch_create_star_items(objects=objects)
    data = _check_resp(resp)
    md = [
        "## 已批量添加收藏项",
        "",
        "已提交批量添加收藏请求。",
        f"- 提交对象数: **{min(len(objects), 1024)}**",
    ]
    _out(md, data if data is not None else resp)


def cmd_star_remove_items(args):
    """批量移除收藏项。POST /v7/drive_star/items/batch_delete"""
    objects = []
    raw_ids = _split_csv(getattr(args, "objects", None))
    if raw_ids:
        objects.extend(raw_ids)

    raw_json = (getattr(args, "objects_json", None) or "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, list):
                objects.extend(parsed)
            else:
                _err("--objects-json 必须是 JSON 数组")
        except json.JSONDecodeError:
            _err("--objects-json 不是合法 JSON")

    item_ids = _split_csv(getattr(args, "item_ids", None)) or []

    if not objects and not item_ids:
        _err("请至少通过 --objects / --objects-json / --item-ids 传入一个对象")

    resp = batch_delete_star_items(objects=objects or None, item_ids=item_ids or None)
    data = _check_resp(resp)
    submitted = len(objects) if objects else len(item_ids)
    md = [
        "## 已批量移除收藏项",
        "",
        "已提交批量移除收藏请求。",
        f"- 提交对象数: **{min(submitted, 1024)}**",
    ]
    _out(md, data if data is not None else resp)


def cmd_tags(args):
    """分页获取自定义标签列表（v7）。GET /v7/drive_labels。"""
    resp = list_drive_labels(
        allotee_type=args.allotee_type or "user",
        allotee_id=getattr(args, "allotee_id", None),
        label_type=args.label_type or "custom",
        page_size=args.page_size or 20,
        page_token=(getattr(args, "page_token", None) or "").strip() or None,
    )
    data = _check_resp(resp)
    items = (data or {}).get("items") or []
    total = (data or {}).get("total")
    next_token = (data or {}).get("next_page_token") or ""

    md = ["## 自定义标签列表（v7）", "", f"本页共 **{len(items)}** 条" + (f"，总计 **{total}** 条" if total is not None else "") + "。"]
    if not items:
        md.append("")
        md.append("暂无标签。")
    else:
        md.append("")
        for i, item in enumerate(items, 1):
            title = item.get("title") or item.get("name") or "-"
            tag_id = item.get("id") or item.get("tag_id") or "-"
            owner = item.get("allotee_type") or item.get("own_type") or "-"
            mtime = item.get("mtime", "-")
            md.append(f"{i}. **{title}**")
            md.append(f"   > tag_id: `{tag_id}` | allotee: {owner} | mtime: {mtime}")
    if next_token:
        md.append("")
        md.append(f"> 翻页可使用 `tags --page-token {next_token}`")
    _out(md, data if data is not None else resp)


def cmd_tag_get(args):
    """获取单个标签信息。GET /v7/drive_labels/{label_id}/meta"""
    if not args.label_id:
        _err("请指定标签 ID，例如: run.py tag-get <label_id>")
    resp = get_drive_label_meta(args.label_id)
    data = _check_resp(resp)
    md = [
        "## 标签详情",
        "",
        f"- **名称**: {data.get('name', '-')}",
        f"- **ID**: `{data.get('id', '-')}`",
        f"- **归属类型**: {data.get('allotee_type', '-')}",
        f"- **标签类型**: {data.get('label_type', '-')}",
        f"- **更新时间**: {data.get('mtime', '-')}",
    ]
    _out(md, data if data is not None else resp)


def cmd_tag_objects(args):
    """分页获取标签下的全部对象。GET /v7/drive_labels/{label_id}/objects"""
    if not args.label_id:
        _err("请指定标签 ID，例如: run.py tag-objects <label_id>")
    resp = list_drive_label_objects(
        label_id=args.label_id,
        page_size=args.page_size or 20,
        page_token=(getattr(args, "page_token", None) or "").strip() or None,
        include_exts=_split_csv(getattr(args, "include_exts", None)),
        exclude_exts=_split_csv(getattr(args, "exclude_exts", None)),
        file_type=args.file_type or "file",
    )
    data = _check_resp(resp)
    items = (data or {}).get("items") or []
    next_token = (data or {}).get("next_page_token") or ""
    md = ["## 标签对象列表", "", f"标签 `{args.label_id}` 本页共 **{len(items)}** 条。", ""]
    if not items:
        md.append("暂无对象。")
    else:
        for i, item in enumerate(items, 1):
            fi = (item.get("file") or item) if isinstance(item, dict) else {}
            obj_id = fi.get("id") or item.get("id") or "-"
            name = fi.get("name") or obj_id or "-"
            obj_type = fi.get("type") or item.get("type") or "-"
            link_url = fi.get("link_url") or item.get("link_url") or ""
            # 标签对象接口常只返回对象 ID；可选自动补全文件元信息。
            if getattr(args, "resolve_meta", True) and obj_type == "file" and (not fi.get("name") or not link_url):
                meta = _resolve_file_meta_for_object_id(obj_id)
                if meta:
                    name = meta.get("name") or name
                    link_url = meta.get("link_url") or link_url
            md.append(f"{i}. **{name}**")
            md.append(f"   > id: `{obj_id}` | type: {obj_type}" + (f" | [链接]({link_url})" if link_url else ""))
    if next_token:
        md.append("")
        md.append(f"> 翻页可使用 `tag-objects {args.label_id} --page-token {next_token}`")
    _out(md, data if data is not None else resp)


def cmd_tag_create(args):
    """创建自定义标签。POST /v7/drive_labels/create"""
    if not args.name:
        _err("请指定标签名称，例如: run.py tag-create --name 我的标签")
    resp = create_drive_label(
        name=args.name,
        allotee_type=args.allotee_type or "user",
        allotee_id=getattr(args, "allotee_id", None),
        label_type=args.label_type or "custom",
        attr=getattr(args, "attr", None),
        rank=getattr(args, "rank", None),
    )
    data = _check_resp(resp)
    md = [
        "## 已创建标签",
        "",
        f"- **名称**: {data.get('name', '-')}",
        f"- **ID**: `{data.get('id', '-')}`",
        f"- **归属类型**: {data.get('allotee_type', '-')}",
        f"- **标签类型**: {data.get('label_type', '-')}",
    ]
    _out(md, data if data is not None else resp)


def cmd_tag_add_objects(args):
    """批量添加标签对象。POST /v7/drive_labels/{label_id}/objects/batch_add"""
    if not args.label_id:
        _err("请指定标签 ID，例如: run.py tag-add-objects <label_id> --objects id1,id2")

    objects = []
    raw_ids = _split_csv(getattr(args, "objects", None))
    if raw_ids:
        objects.extend(raw_ids)

    raw_json = (getattr(args, "objects_json", None) or "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, list):
                objects.extend(parsed)
            else:
                _err("--objects-json 必须是 JSON 数组")
        except json.JSONDecodeError:
            _err("--objects-json 不是合法 JSON")

    if not objects:
        _err("请至少通过 --objects 或 --objects-json 传入一个对象")

    resp = batch_add_drive_label_objects(args.label_id, objects=objects)
    data = _check_resp(resp)
    md = [
        "## 已批量添加标签对象",
        "",
        f"标签 `{args.label_id}` 已提交批量添加请求。",
        f"- 提交对象数: **{min(len(objects), 100)}**",
    ]
    _out(md, data if data is not None else resp)


def cmd_tag_remove_objects(args):
    """批量移除标签对象。POST /v7/drive_labels/{label_id}/objects/batch_remove"""
    if not args.label_id:
        _err("请指定标签 ID，例如: run.py tag-remove-objects <label_id> --objects id1,id2")

    objects = []
    raw_ids = _split_csv(getattr(args, "objects", None))
    if raw_ids:
        objects.extend(raw_ids)

    raw_json = (getattr(args, "objects_json", None) or "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, list):
                objects.extend(parsed)
            else:
                _err("--objects-json 必须是 JSON 数组")
        except json.JSONDecodeError:
            _err("--objects-json 不是合法 JSON")

    if not objects:
        _err("请至少通过 --objects 或 --objects-json 传入一个对象")

    resp = batch_remove_drive_label_objects(args.label_id, objects=objects)
    data = _check_resp(resp)
    md = [
        "## 已批量移除标签对象",
        "",
        f"标签 `{args.label_id}` 已提交批量移除请求。",
        f"- 提交对象数: **{min(len(objects), 100)}**",
    ]
    _out(md, data if data is not None else resp)


def cmd_deleted_list(args):
    """获取回收站文件列表。GET /v7/deleted_files"""
    resp = list_deleted_files(
        drive_id=getattr(args, "drive_id", None),
        with_ext_attrs=getattr(args, "with_ext_attrs", None),
        page_size=args.page_size or 20,
        page_token=(getattr(args, "page_token", None) or "").strip() or None,
        with_drive=getattr(args, "with_drive", None),
    )
    data = _check_resp(resp)
    items = (data or {}).get("items") or []
    next_token = (data or {}).get("next_page_token") or ""
    md = ["## 回收站文件列表", "", f"本页共 **{len(items)}** 条。", ""]
    for i, item in enumerate(items, 1):
        name = item.get("name", "-")
        file_id = item.get("id", "-")
        md.append(f"{i}. **{name}**")
        md.append(f"   > file_id: `{file_id}`")
    if next_token:
        md.append("")
        md.append(f"> 翻页可使用 `deleted-list --page-token {next_token}`")
    _out(md, data if data is not None else resp)


def cmd_deleted_restore(args):
    """还原回收站文件。POST /v7/deleted_files/{file_id}/restore"""
    if not args.file_id:
        _err("请指定 file_id，例如: run.py deleted-restore <file_id>")
    resp = restore_deleted_file(args.file_id)
    data = _check_resp(resp)
    md = ["## 已还原回收站文件", "", f"文件 `{args.file_id}` 已发起还原。"]
    _out(md, data if data is not None else resp)


def cmd_file_move(args):
    """移动文件。POST /v7/drives/{drive_id}/files/{file_id}/move"""
    resp = move_file(
        drive_id=args.drive_id,
        file_id=args.file_id,
        dst_drive_id=args.dst_drive_id,
        dst_parent_id=args.dst_parent_id,
        secure_type=getattr(args, "secure_type", None),
    )
    data = _check_resp(resp)
    md = ["## 已移动文件", "", f"文件 `{args.file_id}` 已移动。"]
    _out(md, data if data is not None else resp)


def cmd_file_copy(args):
    """复制文件。POST /v7/drives/{drive_id}/files/{file_id}/copy"""
    resp = copy_file(
        drive_id=args.drive_id,
        file_id=args.file_id,
        dst_drive_id=args.dst_drive_id,
        dst_parent_id=args.dst_parent_id,
        secure_type=getattr(args, "secure_type", None),
    )
    data = _check_resp(resp)
    md = ["## 已复制文件", "", f"文件 `{args.file_id}` 已发起复制。"]
    _out(md, data if data is not None else resp)

def cmd_file_rename(args):
    """重命名文件（夹）。POST /v7/drives/{drive_id}/files/{file_id}/rename"""
    resp = rename_file(args.drive_id, args.file_id, args.dst_name)
    data = _check_resp(resp)
    md = ["## 已重命名文件", "", f"文件 `{args.file_id}` 已重命名为 **{args.dst_name}**。"]
    _out(md, data if data is not None else resp)


def cmd_file_save_as(args):
    """文件另存为。POST /v7/drives/{drive_id}/files/{file_id}/save_as"""
    resp = save_as_file(
        drive_id=args.drive_id,
        file_id=args.file_id,
        dst_drive_id=args.dst_drive_id,
        dst_parent_id=args.dst_parent_id,
        name=getattr(args, "name", None),
        on_name_conflict=getattr(args, "on_name_conflict", None),
    )
    data = _check_resp(resp)
    md = ["## 已另存为文件", "", f"源文件 `{args.file_id}` 已发起另存为。"]
    _out(md, data if data is not None else resp)


def cmd_file_check_name(args):
    """检查文件名是否存在。POST /v7/drives/{drive_id}/files/{parent_id}/check_name"""
    resp = check_name_exists(args.drive_id, args.parent_id, args.name)
    data = _check_resp(resp)
    exists = (data or {}).get("is_exist")
    md = ["## 文件名检查", "", f"- 名称：**{args.name}**", f"- 是否存在：**{exists}**"]
    _out(md, data if data is not None else resp)


def cmd_file_open_link(args):
    """开启文件分享。POST /v7/drives/{drive_id}/files/{file_id}/open_link"""
    opts = None
    raw_opts = (getattr(args, "opts_json", None) or "").strip()
    if raw_opts:
        try:
            parsed = json.loads(raw_opts)
            if isinstance(parsed, dict):
                opts = parsed
            else:
                _err("--opts-json 必须是 JSON 对象")
        except json.JSONDecodeError:
            _err("--opts-json 不是合法 JSON")
    resp = open_file_link(
        drive_id=args.drive_id,
        file_id=args.file_id,
        opts=opts,
        role_id=getattr(args, "role_id", None),
        scope=getattr(args, "scope", None),
    )
    data = _check_resp(resp)
    md = ["## 已开启文件分享", "", f"文件 `{args.file_id}` 已开启分享。"]
    _out(md, data if data is not None else resp)


def cmd_file_close_link(args):
    """取消文件分享。POST /v7/drives/{drive_id}/files/{file_id}/close_link"""
    resp = close_file_link(args.drive_id, args.file_id, mode=args.mode or "pause")
    data = _check_resp(resp)
    md = ["## 已取消文件分享", "", f"文件 `{args.file_id}` 已关闭分享（mode={args.mode}）。"]
    _out(md, data if data is not None else resp)


def main():
    parser = argparse.ArgumentParser(description="云文档 Drive（V7）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("upload", help="上传文件到云端")
    p.add_argument("file_path", nargs="?", default=None, help="本地文件路径")
    p.add_argument("--filename", "-n", default=None, help="云端文件名（默认与本地文件相同）")
    p.add_argument("--drive", "-d", default="private", help="云盘ID: private(我的云文档), roaming(漫游箱)，默认 private")
    p.add_argument("--parent", "-p", default="root", help="父目录ID，默认 root")
    p.add_argument("--path", default=None, help="目标路径，如 'folder1/folder2'")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("update", help="更新现有文件（上传新版本覆盖，需提供本地文件路径）")
    p.add_argument("file_id", help="文件ID 或 link_id")
    p.add_argument("file_path", help="本地文件路径")
    p.add_argument("--drive", "-d", default="private", help="云盘ID，默认 private")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("write", help="将 Markdown 内容写入文档：智能文档插入，文字/PDF 为转换后覆盖")
    p.add_argument("file_id", help="文件ID 或 link_id")
    p.add_argument("--content", "-c", help="要写入的 Markdown 内容")
    p.add_argument("--file", "-f", help="从本地文件读取 Markdown")
    p.add_argument("--title", help="文档标题（智能文档时使用）")
    p.add_argument("--template", help="DOCX 模板文件路径（文字文档转换时使用）")
    p.add_argument("--mode", "-m", choices=("overwrite", "append"), default="overwrite", help="写入模式：overwrite 从头插入，append 追加，默认 overwrite")
    p.add_argument("--drive", "-d", default="private", help="云盘ID，默认 private")
    p.add_argument("--json", action="store_true", help="仅输出 JSON")
    p.set_defaults(func=cmd_write)

    p = sub.add_parser("list", help="获取文件列表")
    p.add_argument("--drive", "-d", default="private", help="云盘ID，默认 private")
    p.add_argument("--parent", "-p", default="root", help="父目录ID，默认 root")
    p.add_argument("--page-size", "-s", type=int, default=50, help="分页大小，默认50")
    p.add_argument("--page-token", default=None, help="分页 token（从上一次返回的 next_page_token 获取）")
    p.add_argument("--all", action="store_true", help="拉取全部分页（会循环请求直到 next_page_token 为空）")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("get", help="获取文件详情（支持 file_id 或 link_id）")
    p.add_argument("file_id", nargs="?", default=None, help="文件ID 或 link_id")
    p.add_argument("--drive", "-d", default="private", help="云盘ID（仅当传入为 file_id 时生效），默认 private")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("download", help="获取文件下载链接（支持 file_id 或 link_id）")
    p.add_argument("file_id", nargs="?", default=None, help="文件ID 或 link_id")
    p.add_argument("--drive", "-d", default="private", help="云盘ID，默认 private")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("extract", help="按云文档抽取正文（plain/markdown/html），internal GET .../content")
    p.add_argument("file_id", nargs="?", default=None, help="文件ID 或 link_id")
    p.add_argument("--drive", "-d", default="private", help="云盘ID，默认 private")
    p.add_argument("--format", "-f", default="markdown", choices=("plain", "markdown", "html", "kdc"), help="输出格式，默认 markdown")
    p.add_argument("--raw", "-r", action="store_true", help="仅输出正文，无 Markdown 包装与代码块")
    p.add_argument("--json", action="store_true", help="仅输出 JSON（含 file_id、file_name、file_type、format、content）")
    p.add_argument("--type", "-t", choices=("doc", "ap", "pdf", "ppt"), help="文档类型（可选，默认按扩展名检测）")
    p.set_defaults(func=cmd_extract)

    p_read = sub.add_parser("read", help="读取文档为 Markdown/正文（extract 的别名）")
    p_read.add_argument("file_id", nargs="?", default=None, help="文件ID 或 link_id")
    p_read.add_argument("--drive", "-d", default="private", help="云盘ID，默认 private")
    p_read.add_argument("--format", "-f", default="markdown", choices=("plain", "markdown", "html", "kdc"), help="输出格式，默认 markdown")
    p_read.add_argument("--raw", "-r", action="store_true", help="仅输出正文，无 Markdown 包装与代码块")
    p_read.add_argument("--json", action="store_true", help="仅输出 JSON")
    p_read.add_argument("--type", "-t", choices=("doc", "ap", "pdf", "ppt"), help="文档类型（可选）")
    p_read.set_defaults(func=cmd_extract)

    p = sub.add_parser("create", help="统一创建文件/文件夹/快捷方式，POST /v7/drives/{drive_id}/files/{parent_id}/create")
    p.add_argument("file_name", help="名称，如 反馈管理.dbt、文档.otl 或 项目资料")
    p.add_argument("--drive", "-d", default="private", help="云盘：private/roaming/special，默认 private")
    p.add_argument("--parent-id", default=None, dest="parent_id", help="父目录 ID，默认 0")
    p.add_argument("--path", "-p", default=None, help="父路径，如 我的文档 或 我的文档/子目录")
    p.add_argument("--file-type", default="file", dest="file_type", choices=("folder", "file", "shortcut"), help="类型，默认 file")
    p.add_argument("--file-id", default=None, dest="file_id", help="快捷方式引用 file_id（file-type=shortcut 时可用）")
    p.add_argument("--on-conflict", default="rename", choices=("fail", "rename", "overwrite", "replace"), dest="on_conflict", help="重名处理策略，默认 rename")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("link-meta", help="根据 link_id 获取分享链接信息（含 file_id），用于 link_id 换 file_id")
    p.add_argument("link_id", nargs="?", default=None, help="分享链接 ID（如云文档消息中的 link_id）")
    p.set_defaults(func=cmd_link_meta)

    p = sub.add_parser("search", help="搜索云文档文件（简化版，便于 LLM 使用）")
    p.add_argument("keyword", nargs="?", default=None, help="搜索关键词")
    p.add_argument("--type", "-t", default="all", choices=("file_name", "content", "all"), help="搜索类型：文件名/正文/全部，默认 all")
    p.add_argument("--scope", "-s", default=None, help="搜索范围，逗号分隔：all, personal_drive, group_drive, latest, share_by_me, share_to_me, recycle")
    p.add_argument("--page-size", type=int, default=20, help="每页条数，默认 20")
    p.add_argument("--page-token", default=None, help="分页 token（上一页返回的 next_page_token）")
    p.add_argument("--no-total", action="store_true", dest="no_total", help="不返回总条数")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("latest", help="获取最近列表（最近打开/编辑文档）")
    p.add_argument("--page-size", type=int, default=50, help="分页大小，默认 50，最大 500")
    p.add_argument("--page-token", default=None, help="分页 token（上一页返回的 next_page_token）")
    p.add_argument("--with-permission", action="store_const", const=True, default=None, dest="with_permission", help="返回文件操作权限")
    p.add_argument("--with-link", action="store_const", const=True, default=None, dest="with_link", help="返回文件分享信息")
    p.add_argument("--include-exts", default=None, help="按后缀过滤（逗号分隔），例如 md,docx")
    p.add_argument("--exclude-exts", default=None, help="按后缀排除（逗号分隔）")
    p.add_argument("--include-creators", default=None, help="按创建者过滤（逗号分隔）")
    p.add_argument("--exclude-creators", default=None, help="按创建者排除（逗号分隔）")
    p.set_defaults(func=cmd_latest)

    p = sub.add_parser("star", aliases=["favorites"], help="获取收藏列表（GET /v7/drive_star/items）")
    p.add_argument("--page-size", type=int, default=50, help="分页大小，默认 50，最大 200")
    p.add_argument("--page-token", default=None, help="分页 token（上一页返回的 next_page_token）")
    p.add_argument("--order", default=None, choices=("desc", "asc"), help="排序方向：desc/asc")
    p.add_argument("--order-by", default=None, help="排序字段：ctime/file_mtime/source/fname/fsize（按接口为准）")
    p.add_argument("--with-permission", action="store_const", const=True, default=None, dest="with_permission", help="返回文件操作权限")
    p.add_argument("--with-link", action="store_const", const=True, default=None, dest="with_link", help="返回文件分享信息")
    p.add_argument("--include-exts", default=None, help="按后缀过滤（逗号分隔），例如 md,docx")
    p.add_argument("--exclude-exts", default=None, help="按后缀排除（逗号分隔）")
    p.set_defaults(func=cmd_star)

    p = sub.add_parser("star-add-items", help="批量添加收藏项（POST /v7/drive_star/items/batch_create）")
    p.add_argument("--objects", default=None, help="对象 ID 列表，逗号分隔")
    p.add_argument("--objects-json", default=None, help="对象数组 JSON（array）")
    p.add_argument("--items-json", default=None, help="兼容旧字段 items 的数组 JSON（array）")
    p.set_defaults(func=cmd_star_add_items)

    p = sub.add_parser("star-remove-items", help="批量移除收藏项（POST /v7/drive_star/items/batch_delete）")
    p.add_argument("--objects", default=None, help="对象 ID 列表，逗号分隔")
    p.add_argument("--objects-json", default=None, help="对象数组 JSON（array）")
    p.add_argument("--item-ids", default=None, dest="item_ids", help="兼容旧字段 item_ids，逗号分隔")
    p.set_defaults(func=cmd_star_remove_items)

    p = sub.add_parser("tags", aliases=["user-tags"], help="分页获取自定义标签列表（v7: GET /v7/drive_labels）")
    p.add_argument("--allotee-type", default="user", choices=("user", "group", "app"), dest="allotee_type", help="标签归属类型：user/group/app，默认 user")
    p.add_argument("--allotee-id", default=None, dest="allotee_id", help="标签归属 ID；type 为 user 时通常可不传")
    p.add_argument("--label-type", default="custom", choices=("custom", "system"), dest="label_type", help="标签类型：custom/system，默认 custom")
    p.add_argument("--page-size", type=int, default=20, dest="page_size", help="分页大小，默认 20，最大 500")
    p.add_argument("--page-token", default=None, dest="page_token", help="分页 token")
    p.set_defaults(func=cmd_tags)

    p = sub.add_parser("tag-get", help="获取单个标签信息（GET /v7/drive_labels/{label_id}/meta）")
    p.add_argument("label_id", nargs="?", default=None, help="标签 ID")
    p.set_defaults(func=cmd_tag_get)

    p = sub.add_parser("tag-objects", help="分页获取标签下对象（GET /v7/drive_labels/{label_id}/objects）")
    p.add_argument("label_id", nargs="?", default=None, help="标签 ID")
    p.add_argument("--page-size", type=int, default=20, dest="page_size", help="分页大小，默认 20，最大 100")
    p.add_argument("--page-token", default=None, dest="page_token", help="分页 token")
    p.add_argument("--include-exts", default=None, help="按后缀包含过滤（逗号分隔）")
    p.add_argument("--exclude-exts", default=None, help="按后缀排除过滤（逗号分隔）")
    p.add_argument("--file-type", default="file", choices=("file", "folder", "short_cut"), dest="file_type", help="对象类型，默认 file")
    p.add_argument("--no-resolve-meta", action="store_false", default=True, dest="resolve_meta", help="不自动解析对象 ID 对应的文件名/链接")
    p.set_defaults(func=cmd_tag_objects)

    p = sub.add_parser("tag-create", help="创建自定义标签（POST /v7/drive_labels/create）")
    p.add_argument("--name", required=True, help="标签名称")
    p.add_argument("--allotee-type", default="user", choices=("user", "group", "app"), dest="allotee_type", help="标签归属类型，默认 user")
    p.add_argument("--allotee-id", default=None, dest="allotee_id", help="标签归属 ID；type 为 user 时通常可不传")
    p.add_argument("--label-type", default="custom", choices=("custom", "system"), dest="label_type", help="标签类型，默认 custom")
    p.add_argument("--attr", default=None, help="标签自定义属性")
    p.add_argument("--rank", type=float, default=None, help="标签排序值（可选）")
    p.set_defaults(func=cmd_tag_create)

    p = sub.add_parser("tag-add-objects", help="批量添加标签对象（POST /v7/drive_labels/{label_id}/objects/batch_add）")
    p.add_argument("label_id", nargs="?", default=None, help="标签 ID")
    p.add_argument("--objects", default=None, help="对象 ID 列表，逗号分隔")
    p.add_argument("--objects-json", default=None, help="完整对象数组 JSON（array）")
    p.set_defaults(func=cmd_tag_add_objects)

    p = sub.add_parser("tag-remove-objects", help="批量移除标签对象（POST /v7/drive_labels/{label_id}/objects/batch_remove）")
    p.add_argument("label_id", nargs="?", default=None, help="标签 ID")
    p.add_argument("--objects", default=None, help="对象 ID 列表，逗号分隔")
    p.add_argument("--objects-json", default=None, help="完整对象数组 JSON（array）")
    p.set_defaults(func=cmd_tag_remove_objects)

    p = sub.add_parser("deleted-list", help="获取回收站文件列表（GET /v7/deleted_files）")
    p.add_argument("--drive-id", default=None, dest="drive_id", help="按云盘 ID 过滤")
    p.add_argument("--with-ext-attrs", action="store_const", const=True, default=None, dest="with_ext_attrs", help="返回扩展属性")
    p.add_argument("--with-drive", action="store_const", const=True, default=None, dest="with_drive", help="返回 drive 信息")
    p.add_argument("--page-size", type=int, default=20, help="分页大小，默认 20，最大 100")
    p.add_argument("--page-token", default=None, help="分页 token")
    p.set_defaults(func=cmd_deleted_list)

    p = sub.add_parser("deleted-restore", help="还原回收站文件（POST /v7/deleted_files/{file_id}/restore）")
    p.add_argument("file_id", nargs="?", default=None, help="文件 ID")
    p.set_defaults(func=cmd_deleted_restore)

    p = sub.add_parser("file-move", help="移动文件（POST /v7/drives/{drive_id}/files/{file_id}/move）")
    p.add_argument("drive_id", help="源 drive_id")
    p.add_argument("file_id", help="文件 ID")
    p.add_argument("--dst-drive-id", required=True, dest="dst_drive_id", help="目标 drive_id")
    p.add_argument("--dst-parent-id", required=True, dest="dst_parent_id", help="目标目录 parent_id")
    p.add_argument("--secure-type", default=None, dest="secure_type", choices=("decrypt", "encrypt"), help="加密文档迁移策略")
    p.set_defaults(func=cmd_file_move)

    p = sub.add_parser("file-copy", help="复制文件（POST /v7/drives/{drive_id}/files/{file_id}/copy）")
    p.add_argument("drive_id", help="源 drive_id")
    p.add_argument("file_id", help="文件 ID")
    p.add_argument("--dst-drive-id", required=True, dest="dst_drive_id", help="目标 drive_id")
    p.add_argument("--dst-parent-id", required=True, dest="dst_parent_id", help="目标目录 parent_id")
    p.add_argument("--secure-type", default=None, dest="secure_type", choices=("decrypt", "encrypt"), help="加密文档迁移策略")
    p.set_defaults(func=cmd_file_copy)

    p = sub.add_parser("file-rename", help="重命名文件（夹）（POST /v7/drives/{drive_id}/files/{file_id}/rename）")
    p.add_argument("drive_id", help="drive_id")
    p.add_argument("file_id", help="文件 ID")
    p.add_argument("--dst-name", required=True, dest="dst_name", help="新名称")
    p.set_defaults(func=cmd_file_rename)

    p = sub.add_parser("file-save-as", help="文件另存为（POST /v7/drives/{drive_id}/files/{file_id}/save_as）")
    p.add_argument("drive_id", help="源 drive_id")
    p.add_argument("file_id", help="文件 ID")
    p.add_argument("--dst-drive-id", required=True, dest="dst_drive_id", help="目标 drive_id")
    p.add_argument("--dst-parent-id", required=True, dest="dst_parent_id", help="目标目录 parent_id")
    p.add_argument("--name", default=None, help="目标文件名")
    p.add_argument("--on-name-conflict", default=None, dest="on_name_conflict", choices=("fail", "rename", "overwrite", "replace"), help="重名处理策略")
    p.set_defaults(func=cmd_file_save_as)

    p = sub.add_parser("file-check-name", help="检查文件名是否存在（POST /v7/drives/{drive_id}/files/{parent_id}/check_name）")
    p.add_argument("drive_id", help="drive_id")
    p.add_argument("parent_id", help="parent_id")
    p.add_argument("--name", required=True, help="待检查名称")
    p.set_defaults(func=cmd_file_check_name)

    p = sub.add_parser("file-open-link", help="开启文件分享（POST /v7/drives/{drive_id}/files/{file_id}/open_link）")
    p.add_argument("drive_id", help="drive_id")
    p.add_argument("file_id", help="文件 ID")
    p.add_argument("--role-id", default=None, dest="role_id", help="权限角色 ID")
    p.add_argument("--scope", default=None, help="分享范围，如 anyone/company/users")
    p.add_argument("--opts-json", default=None, dest="opts_json", help="分享选项 JSON 对象")
    p.set_defaults(func=cmd_file_open_link)

    p = sub.add_parser("file-close-link", help="取消文件分享（POST /v7/drives/{drive_id}/files/{file_id}/close_link）")
    p.add_argument("drive_id", help="drive_id")
    p.add_argument("file_id", help="文件 ID")
    p.add_argument("--mode", default="pause", choices=("pause", "delete"), help="取消模式，默认 pause")
    p.set_defaults(func=cmd_file_close_link)

    args = parser.parse_args()
    try:
        args.func(args)
    except ValueError as e:
        _err(str(e))
    except Exception as e:
        _err("请求失败: " + str(e))


if __name__ == "__main__":
    main()
