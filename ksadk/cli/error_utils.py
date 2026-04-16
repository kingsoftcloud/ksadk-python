"""CLI 异常分类、结构化错误输出与兼容提示。"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
import sys
from typing import Any, Optional, Sequence, Tuple

import click

from ksadk.cli.dry_run import is_global_dry_run_enabled
from ksadk.cli.ui import emit_json, is_json_output, print_error, print_info

_SERVER_API_ERROR_RE = re.compile(
    r"Server API Error \(Code:\s*([^)]+)\):\s*(.+)",
    re.IGNORECASE,
)

EXIT_CODE_USAGE = 2
EXIT_CODE_RESOLUTION = 3
EXIT_CODE_AUTH = 4
EXIT_CODE_VALIDATION = 5
EXIT_CODE_REMOTE = 6
EXIT_CODE_CANCELLED = 7


@dataclass
class CLIError(Exception):
    """Structured CLI error with stable code and exit semantics."""

    code: str
    message: str
    exit_code: int
    hints: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    context: str | None = None
    show_help: bool = False
    argv: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return self.message


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


def _extract_agentengine_error_details(err: Exception | str) -> dict[str, Any]:
    if isinstance(err, BaseException):
        try:
            from ksadk.api import AgentEngineAPIError

            if isinstance(err, AgentEngineAPIError):
                return dict(getattr(err, "details", {}) or {})
        except Exception:
            pass
    return {}


def _looks_like_missing_cloud_credentials(details: dict[str, Any], msg_lower: str) -> bool:
    remote_code = str(details.get("remote_error_code") or "").strip().lower()
    if remote_code in {"missingaccesskey", "missingsecretkey"}:
        return True
    markers = (
        "missingaccesskey",
        "missingsecretkey",
        "access key is missing",
        "secret key is missing",
    )
    return any(marker in msg_lower for marker in markers)


def _looks_like_invalid_cloud_credentials(details: dict[str, Any], msg_lower: str) -> bool:
    remote_code = str(details.get("remote_error_code") or "").strip().lower()
    if remote_code in {
        "invalidaccesskey",
        "invalidsignature",
        "signaturedoesnotmatch",
        "signaturemismatch",
        "invalidclienttokenid",
        "authfailure",
    }:
        return True
    markers = (
        "invalidaccesskey",
        "invalidsignature",
        "signaturedoesnotmatch",
        "signature mismatch",
        "access key id you provided does not exist",
        "the security token included in the request is invalid",
        "authfailure",
    )
    return any(marker in msg_lower for marker in markers)


def _looks_like_runtime_permission_error(details: dict[str, Any], msg_lower: str, code: int | None) -> bool:
    remote_code = str(details.get("remote_error_code") or "").strip().lower()
    if remote_code in {"accessdenied", "accessdeniedexception", "unauthorized", "unauthorizedoperation"}:
        return True
    if code in {401, 403} and (
        ("权限" in msg_lower)
        or ("permission" in msg_lower)
        or ("access denied" in msg_lower)
        or ("当前账号没有" in msg_lower)
        or ("未授予" in msg_lower)
    ):
        return True
    return False


def _credential_setup_hints() -> list[str]:
    return [
        "请检查当前 shell 或项目 `.env` 中是否设置了 `KSYUN_ACCESS_KEY` / `KSYUN_SECRET_KEY`（兼容 `KS3_ACCESS_KEY` / `KS3_SECRET_KEY`）。",
        "先到 AgentEngine Runtime 控制台确认账号是否具备运行时权限: https://ksp.console.ksyun.com/#/agentEngineRuntime",
        "如当前子账号没有权限，请到 IAM 授权页授权: https://uc.console.ksyun.com/pro/iam/#/permission/authorize",
        "如果还没有金山云 AK/SK，请让主账号先到 IAM 控制台创建子账号并生成访问密钥: https://uc.console.ksyun.com/pro/iam/",
    ]


def _invalid_credential_hints() -> list[str]:
    return [
        "请检查当前 shell 或项目 `.env` 中的 `KSYUN_ACCESS_KEY` / `KSYUN_SECRET_KEY` 是否填写正确，且没有多余空格。",
        "确认该 AK/SK 未被禁用、删除或重置，并且属于当前要操作的金山云账号。",
        "如需确认账号是否具备 AgentEngine Runtime 权限，可先查看: https://ksp.console.ksyun.com/#/agentEngineRuntime",
        "如果凭证属于子账号但仍然被拒绝，请到 IAM 授权页检查授权: https://uc.console.ksyun.com/pro/iam/#/permission/authorize",
    ]


def _runtime_permission_hints() -> list[str]:
    return [
        "请先到 AgentEngine Runtime 控制台确认当前账号是否具备运行时权限: https://ksp.console.ksyun.com/#/agentEngineRuntime",
        "如当前子账号没有权限，请到 IAM 授权页授权: https://uc.console.ksyun.com/pro/iam/#/permission/authorize",
        "如果还没有可用的金山云 AK/SK，请让主账号先到 IAM 控制台创建子账号并生成访问密钥: https://uc.console.ksyun.com/pro/iam/",
    ]


def infer_help_command(argv: Optional[Sequence[str]] = None) -> str:
    args = [a for a in (list(argv) if argv is not None else sys.argv[1:]) if a]
    if args and not args[0].startswith("-"):
        return f"agentengine {args[0]} --help"
    return "agentengine --help"


def _matches_resource_not_found(msg_lower: str, args: Sequence[str], *, resource: str) -> bool:
    if "not found" not in msg_lower:
        return False
    if resource in msg_lower:
        return True
    if not args:
        return False
    if resource == "agent":
        return args[0] in {"agent", "status", "invoke", "delete", "dashboard", "version"}
    return args[0] == resource


def explain_exception(err: Exception, argv: Optional[Sequence[str]] = None) -> Tuple[str, list[str]]:
    cli_error = cli_error_from_exception(err, argv=argv)
    return cli_error.message, cli_error.hints


def usage_error(
    message: str,
    *,
    hints: Sequence[str] | None = None,
    details: dict[str, Any] | None = None,
    context: str | None = None,
    show_help: bool = False,
    argv: Sequence[str] | None = None,
) -> CLIError:
    return CLIError(
        code="usage_error",
        message=message,
        exit_code=EXIT_CODE_USAGE,
        hints=list(hints or []),
        details=dict(details or {}),
        context=context,
        show_help=show_help,
        argv=list(argv or []),
    )


def resolution_error(
    message: str,
    *,
    hints: Sequence[str] | None = None,
    details: dict[str, Any] | None = None,
    context: str | None = None,
    show_help: bool = False,
    argv: Sequence[str] | None = None,
) -> CLIError:
    return CLIError(
        code="resolution_error",
        message=message,
        exit_code=EXIT_CODE_RESOLUTION,
        hints=list(hints or []),
        details=dict(details or {}),
        context=context,
        show_help=show_help,
        argv=list(argv or []),
    )


def auth_error(
    message: str = "鉴权失败。",
    *,
    hints: Sequence[str] | None = None,
    details: dict[str, Any] | None = None,
    context: str | None = None,
    argv: Sequence[str] | None = None,
) -> CLIError:
    return CLIError(
        code="auth_error",
        message=message,
        exit_code=EXIT_CODE_AUTH,
        hints=list(
            hints
            or [
                "请检查 KSYUN_ACCESS_KEY / KSYUN_SECRET_KEY 是否正确。",
                "如使用子账号，请确认已授予对应接口权限。",
            ]
        ),
        details=dict(details or {}),
        context=context,
        argv=list(argv or []),
    )


def validation_error(
    message: str,
    *,
    hints: Sequence[str] | None = None,
    details: dict[str, Any] | None = None,
    context: str | None = None,
    argv: Sequence[str] | None = None,
) -> CLIError:
    return CLIError(
        code="validation_error",
        message=message,
        exit_code=EXIT_CODE_VALIDATION,
        hints=list(hints or []),
        details=dict(details or {}),
        context=context,
        argv=list(argv or []),
    )


def remote_error(
    message: str,
    *,
    hints: Sequence[str] | None = None,
    details: dict[str, Any] | None = None,
    context: str | None = None,
    argv: Sequence[str] | None = None,
) -> CLIError:
    return CLIError(
        code="remote_error",
        message=message,
        exit_code=EXIT_CODE_REMOTE,
        hints=list(hints or []),
        details=dict(details or {}),
        context=context,
        argv=list(argv or []),
    )


def cancelled_error(
    message: str = "已取消。",
    *,
    hints: Sequence[str] | None = None,
    details: dict[str, Any] | None = None,
    context: str | None = None,
    argv: Sequence[str] | None = None,
) -> CLIError:
    return CLIError(
        code="cancelled",
        message=message,
        exit_code=EXIT_CODE_CANCELLED,
        hints=list(hints or []),
        details=dict(details or {}),
        context=context,
        argv=list(argv or []),
    )


def unsupported_json_output_error(command: str, *, suggestion: str | None = None) -> CLIError:
    hints = [suggestion] if suggestion else []
    return usage_error(
        f"`{command}` 暂不支持 `--output json`。",
        hints=hints,
        details={"command": command, "output": "json"},
    )


def ensure_json_output_supported(command: str, *, suggestion: str | None = None) -> None:
    if is_json_output():
        abort_with_cli_error(unsupported_json_output_error(command, suggestion=suggestion))


def unsupported_dry_run_error(command: str, *, suggestion: str | None = None) -> CLIError:
    hints = [suggestion] if suggestion else []
    return usage_error(
        f"`{command}` 暂不支持 `--dry-run`。",
        hints=hints,
        details={"command": command, "dry_run": True},
    )


def ensure_dry_run_supported(command: str, *, suggestion: str | None = None) -> None:
    if is_global_dry_run_enabled():
        abort_with_cli_error(unsupported_dry_run_error(command, suggestion=suggestion))


def cli_error_from_exception(
    err: Exception,
    *,
    context: str | None = None,
    argv: Optional[Sequence[str]] = None,
    show_help: bool = False,
) -> CLIError:
    if isinstance(err, CLIError):
        if context and not err.context:
            err.context = context
        if show_help and not err.show_help:
            err.show_help = True
        if argv and not err.argv:
            err.argv = list(argv)
        return err

    if isinstance(err, click.Abort):
        return cancelled_error(context=context, argv=argv)
    if isinstance(err, click.BadParameter):
        return usage_error(str(err), context=context, show_help=show_help, argv=argv)
    if isinstance(err, click.UsageError):
        return usage_error(str(err), context=context, show_help=True, argv=argv)
    if isinstance(err, FileNotFoundError):
        return resolution_error(str(err), context=context, argv=argv)
    if isinstance(err, ValueError):
        return validation_error(str(err), context=context, argv=argv)

    code, msg = parse_server_api_error(err)
    details = _extract_agentengine_error_details(err)
    args = [a for a in (list(argv) if argv is not None else sys.argv[1:]) if a]
    msg_lower = (msg or "").lower()

    summary = msg or err.__class__.__name__
    hints: list[str] = []
    error_code = "remote_error"
    exit_code = EXIT_CODE_REMOTE

    if code == 404 and len(args) >= 2 and args[0] == "dashboard" and args[1] == "share":
        summary = "未找到 Dashboard 分享链接或目标 Agent。"
        hints.append("请先执行 `agentengine dashboard share list --agent <AgentName|AgentId>` 查看分享链接。")
        hints.append("如需先确认 Agent，请执行 `agentengine agent list`。")
        error_code = "resolution_error"
        exit_code = EXIT_CODE_RESOLUTION
    elif code == 404 and args and args[0] == "version":
        summary = "未找到目标 Agent 或版本。"
        hints.append("请先执行 `agentengine agent list` 确认目标 Agent。")
        hints.append("然后执行 `agentengine version list --agent <AgentName|AgentId>` 查看版本。")
        error_code = "resolution_error"
        exit_code = EXIT_CODE_RESOLUTION
    elif code == 404 and _matches_resource_not_found(msg_lower, args, resource="agent"):
        summary = "未找到 Agent。"
        hints.append("请确认 Agent 名称/ID 是否正确，可先执行 `agentengine agent list` 查看已部署 Agent。")
        if len(args) >= 2 and args[0] == "dashboard" and args[1] == "list":
            hints.append("`agentengine dashboard list` 不是有效命令。")
            hints.append("如果要查看分享链接，请使用 `agentengine dashboard share list --agent <AgentName|AgentId>`。")
        elif args and args[0] == "dashboard":
            hints.append("可显式指定 Agent：`agentengine dashboard open --agent <AgentName|AgentId>`。")
        error_code = "resolution_error"
        exit_code = EXIT_CODE_RESOLUTION
    elif code == 404 and _matches_resource_not_found(msg_lower, args, resource="mcp"):
        summary = "未找到 MCP。"
        hints.append("请确认 MCP 名称/ID 是否正确，可先执行 `agentengine mcp list` 查看已部署 MCP。")
        error_code = "resolution_error"
        exit_code = EXIT_CODE_RESOLUTION
    elif code == 404 and _matches_resource_not_found(msg_lower, args, resource="openclaw"):
        summary = "未找到 OpenClaw。"
        hints.append("请确认 OpenClaw 名称/ID 是否正确，可先执行 `agentengine openclaw list` 查看已部署实例。")
        error_code = "resolution_error"
        exit_code = EXIT_CODE_RESOLUTION
    elif _looks_like_missing_cloud_credentials(details, msg_lower):
        return auth_error(
            message="未检测到金山云 AK/SK。",
            hints=_credential_setup_hints(),
            details={"server_code": code, **details},
            context=context,
            argv=argv,
        )
    elif _looks_like_invalid_cloud_credentials(details, msg_lower):
        return auth_error(
            message="金山云 AK/SK 不正确或已失效。",
            hints=_invalid_credential_hints(),
            details={"server_code": code, **details},
            context=context,
            argv=argv,
        )
    elif _looks_like_runtime_permission_error(details, msg_lower, code):
        return auth_error(
            message="当前金山云账号没有 AgentEngine Runtime 所需权限。",
            hints=_runtime_permission_hints(),
            details={"server_code": code, **details},
            context=context,
            argv=argv,
        )
    elif code in {401, 403}:
        return auth_error(context=context, argv=argv)
    elif code == 429:
        summary = "请求过于频繁。"
        hints.append("请稍后重试，或降低并发/轮询频率。")
    elif code is not None and code >= 500:
        summary = f"服务端暂时不可用 (Code: {code})。"
        hints.append("请稍后重试；若持续失败请联系平台侧排查。")
    elif code is not None and 400 <= code < 500:
        error_code = "validation_error"
        exit_code = EXIT_CODE_VALIDATION
        summary = f"服务端返回错误 (Code: {code}): {msg}"

    return CLIError(
        code=error_code,
        message=summary,
        exit_code=exit_code,
        hints=hints,
        details=({"server_code": code, **details} if code is not None else dict(details)),
        context=context,
        show_help=show_help,
        argv=list(argv or []),
    )


def emit_cli_error(err: CLIError) -> None:
    hints = list(err.hints)
    if err.show_help:
        hints.append(f"运行 `{infer_help_command(argv=err.argv)}` 查看参数说明。")

    if is_json_output():
        emit_json(
            {
                "ok": False,
                "error": {
                    "code": err.code,
                    "message": err.message,
                    "details": err.details or {},
                },
                "hints": hints,
            }
        )
        return

    if err.context:
        print_error(f"{err.context}: {err.message}")
    else:
        print_error(f"错误: {err.message}")
    for hint in hints:
        print_info(f"提示: {hint}")


def abort_with_cli_error(
    err: Exception | CLIError,
    *,
    context: str | None = None,
    argv: Optional[Sequence[str]] = None,
    show_help: bool = False,
) -> None:
    """Emit a structured CLI error and terminate with its canonical exit code."""
    cli_error = cli_error_from_exception(
        err,
        context=context,
        argv=argv,
        show_help=show_help,
    )
    emit_cli_error(cli_error)
    raise SystemExit(cli_error.exit_code)


def print_exception(
    context: Optional[str],
    err: Exception,
    *,
    show_help: bool = False,
    argv: Optional[Sequence[str]] = None,
) -> None:
    emit_cli_error(
        cli_error_from_exception(
            err,
            context=context,
            argv=argv,
            show_help=show_help,
        )
    )
