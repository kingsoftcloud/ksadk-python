"""CLI 异常格式化与输出工具。"""

from __future__ import annotations

import os
import re
import sys
from typing import Optional, Sequence, Tuple

from ksadk.cli.ui import print_error, print_info

_SERVER_API_ERROR_RE = re.compile(
    r"Server API Error \(Code:\s*([^)]+)\):\s*(.+)",
    re.IGNORECASE,
)


def is_debug_mode_enabled() -> bool:
    return os.getenv("AGENTENGINE_DEBUG", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def parse_server_api_error(err: Exception | str) -> Tuple[Optional[int], str]:
    if isinstance(err, BaseException):
        try:
            from ksadk.api import AgentEngineAPIError

            if isinstance(err, AgentEngineAPIError):
                return err.code, err.message
        except Exception:
            pass

    text = str(err or "").strip()
    match = _SERVER_API_ERROR_RE.search(text)
    if not match:
        if text:
            return None, text
        if isinstance(err, BaseException):
            return None, err.__class__.__name__
        return None, "Unknown error"

    raw_code = match.group(1).strip()
    msg = match.group(2).strip() or "Unknown API error"
    try:
        code = int(raw_code)
    except ValueError:
        code = None
    return code, msg


def infer_help_command(argv: Optional[Sequence[str]] = None) -> str:
    args = [a for a in (list(argv) if argv is not None else sys.argv[1:]) if a]
    if args and not args[0].startswith("-"):
        return f"agentengine {args[0]} --help"
    return "agentengine --help"


def explain_exception(err: Exception, argv: Optional[Sequence[str]] = None) -> Tuple[str, list[str]]:
    code, msg = parse_server_api_error(err)
    args = [a for a in (list(argv) if argv is not None else sys.argv[1:]) if a]
    msg_lower = (msg or "").lower()

    summary = msg or err.__class__.__name__
    hints: list[str] = []

    if code == 404 and "agent" in msg_lower and "not found" in msg_lower:
        summary = "未找到 Agent。"
        hints.append("请确认 Agent 名称/ID 是否正确，可先执行 `agentengine status` 查看已部署 Agent。")
        if len(args) >= 2 and args[0] == "dashboard" and args[1] == "list":
            hints.append("`agentengine dashboard list` 会把 `list` 识别为 Agent 名称。")
            hints.append("如果要查看分享链接，请使用 `agentengine dashboard share list --agent <AgentName|AgentId>`。")
        elif args and args[0] == "dashboard":
            hints.append("可显式指定 Agent：`agentengine dashboard --agent <AgentName|AgentId>`。")
    elif code in {401, 403}:
        summary = "鉴权失败。"
        hints.append("请检查 KSYUN_ACCESS_KEY / KSYUN_SECRET_KEY 是否正确。")
        hints.append("如使用子账号，请确认已授予对应接口权限。")
    elif code == 429:
        summary = "请求过于频繁。"
        hints.append("请稍后重试，或降低并发/轮询频率。")
    elif code is not None and code >= 500:
        summary = f"服务端暂时不可用 (Code: {code})。"
        hints.append("请稍后重试；若持续失败请联系平台侧排查。")
    elif code is not None:
        summary = f"服务端返回错误 (Code: {code}): {msg}"

    return summary, hints


def print_exception(
    context: Optional[str],
    err: Exception,
    *,
    show_help: bool = False,
    argv: Optional[Sequence[str]] = None,
) -> None:
    summary, hints = explain_exception(err, argv=argv)
    if context:
        print_error(f"{context}: {summary}")
    else:
        print_error(f"错误: {summary}")

    for hint in hints:
        print_info(f"提示: {hint}")

    if show_help:
        print_info(f"帮助: 运行 `{infer_help_command(argv=argv)}` 查看参数说明。")
