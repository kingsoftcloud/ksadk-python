"""用户可读错误消息翻译单测。"""

from __future__ import annotations

from ksadk.harness.model_provider import ClassifiedModelFailure, ModelFailureKind
from ksadk.harness.user_errors import user_facing_error


def test_rate_limit_translates_to_chinese():
    failure = ClassifiedModelFailure(ModelFailureKind.RATE_LIMIT, 429)
    msg = user_facing_error(failure=failure)
    assert "频率受限" in msg
    assert "429" not in msg


def test_timeout_translates():
    failure = ClassifiedModelFailure(ModelFailureKind.TIMEOUT, 504)
    msg = user_facing_error(failure=failure)
    assert "超时" in msg


def test_context_overflow_translates():
    failure = ClassifiedModelFailure(ModelFailureKind.CONTEXT_LENGTH, 400)
    msg = user_facing_error(failure=failure)
    assert "压缩" in msg


def test_tool_error_context():
    msg = user_facing_error(context="tool")
    assert "工具" in msg
    assert "exception" not in msg.lower()
    assert "trace" not in msg.lower()


def test_approval_denied_context():
    msg = user_facing_error(context="approval")
    assert "审批" in msg
    assert "拒绝" in msg


def test_raw_exception_not_exposed():
    """用户消息不含 raw exception / stack trace。"""
    error = RuntimeError("Connection refused: postgres://user:secret@host:5432/db")
    msg = user_facing_error(error=error)
    assert "secret" not in msg
    assert "postgres://" not in msg
    assert "Connection refused" not in msg


def test_unknown_failure_has_fallback():
    failure = ClassifiedModelFailure(ModelFailureKind.UNKNOWN, 500)
    msg = user_facing_error(failure=failure)
    assert "重试" in msg
