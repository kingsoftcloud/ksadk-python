#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通讯录按人名搜索：调用 V7 企业用户搜索，输出 Markdown + JSON。
同名用户会全部返回。
需在 wps365-skill 根目录执行，并设置环境变量 wps_sid。
用法: python skills/contacts/run.py search <人名>
      或 python skills/contacts/run.py search --keyword "人名"
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

from wpsv7client import search_users  # noqa: E402


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


def _user_display_name(u):
    return u.get("user_name") or u.get("nick_name") or u.get("name") or u.get("id") or "-"


def _user_dept_str(u):
    depts = u.get("dept_path")
    if not depts:
        return "-"
    return depts.split("/")[-1]


def cmd_search(args):
    keyword = args.keyword or (args.name if getattr(args, "name", None) else "")
    if not keyword or not str(keyword).strip():
        _err("请指定要搜索的人名，例如: run.py search 张三")
    resp = search_users(keyword=keyword.strip())
    data = _check_resp(resp)
    if not isinstance(data, dict):
        data = {}
    items = data.get("items") or []
    md = ["## 通讯录·按人名搜索", "", f"根据「{keyword}」找到 **{len(items)}** 个用户。"]
    if not items:
        md.append("")
        md.append("未找到匹配用户。")
    else:
        md.append("")
        for i, u in enumerate(items[:50], 1):
            name = _user_display_name(u)
            uid = u.get("id", "-")
            dept = _user_dept_str(u)
            email = u.get("email") or "-"
            md.append(f"{i}. **{name}** | 用户ID: `{uid}` | 部门: {dept} | 邮箱: {email}")
        if len(items) > 50:
            md.append("")
            md.append(f"（仅展示前 50 条，共 {len(items)} 条）")
    _out(md, data)


def main():
    parser = argparse.ArgumentParser(description="通讯录按人名搜索（V7）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("search", help="按人名搜索用户，同名返回多条")
    p.add_argument("name", nargs="?", default=None, help="要搜索的人名")
    p.add_argument("--keyword", "-k", default=None, help="要搜索的人名（与 positional 二选一）")
    p.set_defaults(func=cmd_search)

    args = parser.parse_args()
    try:
        args.func(args)
    except ValueError as e:
        _err(str(e))
    except Exception as e:
        _err("请求失败: " + str(e))


if __name__ == "__main__":
    main()
