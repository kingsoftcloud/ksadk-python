#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
查询当前用户信息：调用 GET /v7/users/current，输出 Markdown + JSON。
需在 wps365-skill 根目录执行，并设置环境变量 wps_sid。
"""
import json
import sys
from pathlib import Path

# 保证终端输出 UTF-8，避免中文与 Markdown 符号乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 保证可导入 wpsv7client（以 repo 根为 wps365-skill）
_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from wpsv7client import get_current_user


def main() -> None:
    try:
        resp = get_current_user()
        if resp.get("code") != 0:
            print("## 错误\n\n" + (resp.get("msg") or resp.get("message") or "未知错误"), file=sys.stderr)
            sys.exit(1)
        data = resp.get("data") or {}
    except ValueError as e:
        print("## 错误\n\n" + str(e), file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print("## 请求失败\n\n" + str(e), file=sys.stderr)
        sys.exit(1)

    # Markdown 摘要
    name = data.get("user_name") or data.get("nick_name") or "-"
    uid = data.get("id", "-")
    cid = data.get("company_id", "-")
    depts = data.get("depts") or []
    dept_str = "、".join((d.get("name") or d.get("id") or "") for d in depts[:5]) or "无"

    # 开头空行，便于部分渲染器将整段识别为 Markdown
    lines = [
        "",
        "## 当前用户",
        "",
        f"- **用户**：{name}",
        f"- **用户ID**：{uid}",
        f"- **企业ID**：{cid}",
        f"- **部门**：{dept_str}",
        "",
        "## 原始数据 (JSON)",
        "",
        "```json",
        json.dumps(data, ensure_ascii=False, indent=2),
        "```",
    ]
    out = "\n".join(lines)
    print(out)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
