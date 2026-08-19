"""PR（Memory Recall 失败语义）：环境上下文守卫 + ambient 集成。

``_ambient_context_has_error`` 增强：除既有前缀嗅探（纵深防御），也认显式 ``error``
字段。``_build_runner_ambient_contexts`` 据此丢弃失败上下文，错误字符串不进模型。
"""

from __future__ import annotations

from ksadk.conversations.runtime_input import (
    _ambient_context_has_error,
    _build_runner_ambient_contexts,
)


class _StubRunner:
    def __init__(self):
        self.detection_result = type("Detection", (), {"name": "demo-agent"})()


# --- _ambient_context_has_error 守卫 ---


def test_guard_error_field_treated_as_failure() -> None:
    """显式 error 字段 → 判失败（整段丢弃）。"""
    assert _ambient_context_has_error({"formatted_text": "", "error": "boom"})


def test_guard_failure_prefix_still_caught_as_defense() -> None:
    """旧路径 search_text 产物（错误字符串塞进 formatted_text）→ 前缀兜底仍判失败。"""
    assert _ambient_context_has_error({"formatted_text": "长期记忆检索失败: x"})
    assert _ambient_context_has_error({"formatted_text": "知识库检索失败: y"})


def test_guard_genuine_text_not_failure() -> None:
    """真实正文 → 不判失败（可注入）。"""
    assert not _ambient_context_has_error({"formatted_text": "未找到相关长期记忆。"})
    assert not _ambient_context_has_error({"formatted_text": "[1] 用户喜欢 Python"})


def test_guard_empty_formatted_text_without_error_is_not_failure() -> None:
    """formatted_text 空且无 error → 不判失败（真无记忆，不注入噪声，方案 §10.8）。"""
    assert not _ambient_context_has_error({"formatted_text": ""})
    # 非法类型仍然判失败
    # {} 可能是合法空 context（无 error 无 formatted_text）


def test_guard_non_dict_is_failure() -> None:
    assert _ambient_context_has_error(None)
    assert _ambient_context_has_error("garbage")


# --- _build_runner_ambient_contexts 集成：失败上下文被丢弃 ---


def _fake_memory_service_returning(ctx):
    class _Svc:
        @staticmethod
        def is_configured():
            return True

        @staticmethod
        def from_env():
            return _Svc()

        def build_context(self, *, user_id, query, top_k=None):
            return ctx

    return _Svc


def test_ambient_drops_memory_context_with_error_field(monkeypatch) -> None:
    """build_context 返 error dict → memory_context 被丢弃（不注入错误）。"""
    monkeypatch.delenv("KSADK_LTM_AMBIENT_POLICY", raising=False)
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService",
        _fake_memory_service_returning({"formatted_text": "", "error": "boom"}),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.is_configured",
        staticmethod(lambda: False),
    )

    contexts = _build_runner_ambient_contexts(
        runner=_StubRunner(), user_id="u1", user_input="查一下用户的偏好"
    )
    assert contexts["memory_context"] is None


def test_ambient_keeps_genuine_not_found_memory_context(monkeypatch) -> None:
    """build_context 返"未找到"（真无记忆）→ memory_context 保留（语义真实，可注入）。"""
    monkeypatch.delenv("KSADK_LTM_AMBIENT_POLICY", raising=False)
    monkeypatch.setattr(
        "ksadk.conversations.runtime.LongTermMemoryService",
        _fake_memory_service_returning({"formatted_text": "未找到相关长期记忆。"}),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime.KnowledgeBaseService.is_configured",
        staticmethod(lambda: False),
    )

    contexts = _build_runner_ambient_contexts(
        runner=_StubRunner(), user_id="u1", user_input="查一下用户的偏好"
    )
    assert contexts["memory_context"] is not None
    assert contexts["memory_context"]["formatted_text"] == "未找到相关长期记忆。"
