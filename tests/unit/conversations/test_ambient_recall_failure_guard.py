"""PR（Memory Recall 失败语义）：守卫与环境构建器层面不得让错误上下文进 payload。

build_context 层的失败分支（error 字段 / last_error）已由
``tests/unit/memory/test_recall_failure_semantics.py`` 与
``tests/unit/knowledge_base/test_kb_recall_failure.py`` 覆盖。这里补守卫
``_ambient_context_has_error`` 与环境构建器 ``_build_runner_ambient_contexts``
两个未被覆盖的环节：

- 守卫：``error`` 字段非空 → 丢弃（核心不变式）；前缀兜底保留；
  真无记忆正文、非 dict、空正文各自判定。
- 环境构建器：build_context 返 error 字段（后端抛错/吞错）→ 上下文被丢弃，
  不进 payload；真无记忆正文 → 保留（旧行为，语义真实）。
"""

from __future__ import annotations

from ksadk.conversations.runtime_input import (
    _ambient_context_has_error,
    _build_runner_ambient_contexts,
)
from ksadk.memory.adk.backends.base_ltm_backend import BaseLongTermMemoryBackend
from ksadk.memory.service import LongTermMemoryService

# --- 守卫 ---


def test_guard_drops_explicit_error_field() -> None:
    """build_context 失败返 formatted_text="" + error → 守卫丢弃（错误不进 payload）。"""
    assert _ambient_context_has_error({"formatted_text": "", "error": "boom"}) is True


def test_guard_drops_failure_prefix_as_defense_in_depth() -> None:
    """纵深防御：直接调 search_text 拼上下文的残留路径仍按前缀兜底。"""
    assert _ambient_context_has_error({"formatted_text": "长期记忆检索失败: x"}) is True
    assert _ambient_context_has_error({"formatted_text": "知识库检索失败: y"}) is True


def test_guard_passes_truthful_text() -> None:
    """真无记忆/正常正文（非失败）通过守卫，可注入。"""
    assert _ambient_context_has_error({"formatted_text": "未找到相关长期记忆。"}) is False
    assert _ambient_context_has_error({"formatted_text": "正常记忆正文"}) is False


def test_guard_drops_non_dict_and_empty_text() -> None:
    assert _ambient_context_has_error(None) is True
    assert _ambient_context_has_error({}) is True
    assert _ambient_context_has_error({"formatted_text": ""}) is True


# --- 环境构建器 ---


class _RaisingBackend(BaseLongTermMemoryBackend):
    """search_memory 抛错（路径 A）。"""

    index: str = "test"

    def save_memory(self, user_id, event_strings, **kwargs):  # pragma: no cover
        return True

    def search_memory(self, user_id, query, top_k=5, **kwargs):
        raise RuntimeError("boom: backend unreachable")


class _SwallowBackend(BaseLongTermMemoryBackend):
    """search_memory 吞错返空 + 设 last_error（路径 C）。"""

    index: str = "test"

    def save_memory(self, user_id, event_strings, **kwargs):  # pragma: no cover
        return True

    def search_memory(self, user_id, query, top_k=5, **kwargs):
        self.last_error = "boom: swallowed network error"
        return []


class _GenuineEmptyBackend(BaseLongTermMemoryBackend):
    """search_memory 正常返空（真无记忆，无失败）。"""

    index: str = "test"

    def save_memory(self, user_id, event_strings, **kwargs):  # pragma: no cover
        return True

    def search_memory(self, user_id, query, top_k=5, **kwargs):
        self.last_error = ""
        return []


class _StubRunner:
    """足够通过 _should_use_platform_ambient_context 的 runner 占位。"""

    detection_result = type(
        "D", (), {"name": "stub", "type": type("T", (), {"value": "langgraph"})()}
    )()


def _patch_ltm(monkeypatch, backend: BaseLongTermMemoryBackend) -> None:
    monkeypatch.delenv("KSADK_LTM_AMBIENT_POLICY", raising=False)
    monkeypatch.setattr(
        "ksadk.conversations.runtime_input.LongTermMemoryService.is_configured",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime_input.LongTermMemoryService.from_env",
        staticmethod(lambda: LongTermMemoryService(backend=backend, index="test")),
    )
    monkeypatch.setattr(
        "ksadk.conversations.runtime_input.KnowledgeBaseService.is_configured",
        staticmethod(lambda: False),
    )


def test_ambient_builder_drops_memory_error_on_raise(monkeypatch) -> None:
    """后端抛错（路径 A）→ build_context 返 error → 环境构建器丢弃，不进 payload。"""
    _patch_ltm(monkeypatch, _RaisingBackend())
    contexts = _build_runner_ambient_contexts(
        runner=_StubRunner(), user_id="u", user_input="回忆一下我之前的偏好"
    )
    assert contexts["memory_context"] is None
    assert contexts["kb_context"] is None


def test_ambient_builder_drops_memory_error_on_swallowed_empty(monkeypatch) -> None:
    """后端吞错返空（路径 C）→ build_context 返 error → 环境构建器丢弃，
    不再把"未找到"伪装正文注入。"""
    _patch_ltm(monkeypatch, _SwallowBackend())
    contexts = _build_runner_ambient_contexts(
        runner=_StubRunner(), user_id="u", user_input="回忆一下我之前的偏好"
    )
    assert contexts["memory_context"] is None


def test_ambient_builder_keeps_truthful_not_found(monkeypatch) -> None:
    """真无记忆（返空且无 last_error）→ "未找到…"正文保留（语义真实，旧行为）。"""
    _patch_ltm(monkeypatch, _GenuineEmptyBackend())
    contexts = _build_runner_ambient_contexts(
        runner=_StubRunner(), user_id="u", user_input="回忆一下我之前的偏好"
    )
    assert contexts["memory_context"] is not None
    assert contexts["memory_context"]["formatted_text"] == "未找到相关长期记忆。"
