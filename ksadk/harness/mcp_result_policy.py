"""Framework-free MCP result disclosure policy shared by managed runtimes."""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 平台托管 Runtime 的基础敏感数据默认规则（组织可覆盖或接 DLP）：
#: 中国大陆身份证号 / 16-19 位银行卡号 / 1[3-9] 开头手机号。
_DEFAULT_SENSITIVE_PATTERNS = (
    # A long fractional component (for example a vector-search score such as
    # ``0.9619389772415161``) is not an identity or payment-card number.  The
    # previous word-boundary patterns started matching immediately after the
    # decimal point and offloaded otherwise harmless MCP results with an empty
    # summary, leaving the model unable to read knowledge-base results.
    r"(?<![\d.])\d{17}[\dXx](?![\d.])",
    r"(?<![\d.])\d{16,19}(?![\d.])",
    r"(?<![\d.])1[3-9]\d{9}(?![\d.])",
)


@dataclass(frozen=True)
class McpOffloadPolicy:
    """P1 大结果 Offload 策略。

    - ``single_result_threshold_bytes``：单次工具结果超过该字节数 → 外置；
    - ``sensitive_patterns``：命中任一正则 → 无论大小强制外置，且摘要置空
      （敏感明文不留在 Context）。默认开启平台基础规则（身份证/银行卡/
      手机号），组织可通过自定义 policy 覆盖或接入 DLP/分类服务；
    - ``run_total_quota_bytes``：Run 累计外置字节配额，超出后新的超大结果
      明确报错降级（不回填 Context）。
    """

    enabled: bool = True
    single_result_threshold_bytes: int = 4096
    run_total_quota_bytes: int = 10 * 1024 * 1024
    summary_chars: int = 500
    sensitive_patterns: tuple[str, ...] = _DEFAULT_SENSITIVE_PATTERNS

    def sensitive_match(self, content: str) -> str | None:
        for pattern in self.sensitive_patterns:
            if re.search(pattern, content):
                return pattern
        return None
