"""可进入运行事件和日志的异常摘要。"""

from __future__ import annotations

import re
import traceback

_URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"']+")
_AUTH = re.compile(r"\b(?:bearer|basic)\s+[^\s,;\"']+", re.IGNORECASE)
_PRIVATE_HEADER = re.compile(
    r"\b(?:[A-Za-z0-9]+[_-])*(?:set-cookie|cookie|authorization)[\"']?"
    r"\s*[:=]\s*[^\r\n]+",
    re.IGNORECASE,
)
_CREDENTIAL = re.compile(
    r"\b(?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|access[_-]?key(?:[_-]?id)?|"
    r"secret[_-]?access[_-]?key|"
    r"authorization|password|passwd|(?:access[_-]?|refresh[_-]?|session[_-]?)token|"
    r"secret|client[_-]?secret|_authToken)[\"']?\s*[:=]\s*"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;}\]]+)",
    re.IGNORECASE,
)
_API_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE)


def safe_exception_locations(error: BaseException) -> list[dict[str, object]]:
    """Exception types and source locations, without text, code, or locals."""
    chain = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(chain) < 5:
        seen.add(id(current))
        chain.append({"type": type(current).__name__, "locations": [
            f"{frame.name}:{frame.lineno}" for frame in traceback.extract_tb(current.__traceback__)
        ]})
        current = current.__cause__ or current.__context__
    return chain


def safe_exception_detail(error: Exception, *, limit: int = 2048) -> str:
    """保留异常类型与诊断文本，去掉 URL、凭证和 cookie 后再截断。

    URL 整体移除，避免遗漏签名下载地址中厂商各异的鉴权参数。
    不使用 exc_info，防止日志在安全摘要后再次打印原始异常。
    """
    detail = f"{type(error).__name__}: {error}"
    for pattern in (_URL, _AUTH, _PRIVATE_HEADER, _CREDENTIAL, _API_KEY):
        detail = pattern.sub("[REDACTED]", detail)
    return detail[:limit]
