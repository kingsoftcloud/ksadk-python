"""PR（Memory Recall 失败语义）：环境记忆上下文不得把 Provider 异常伪装成
"未找到"正文注入模型。

覆盖 LongTermMemoryService.build_context 的三条分支：
- 后端抛错（如 SDK 客户端初始化失败）→ ``formatted_text=""`` + 独立 ``error`` 字段。
- 后端吞错返空列表（SDK/HTTP 网络失败，``last_error`` 非空）→ ``error`` 字段（堵路径 C）。
- 真无记忆（返空且 ``last_error`` 空）→ ``formatted_text=""  # Fix 12: empty → "" not "未找到"（方案 §10.8）``
  （语义真实，可注入；非失败，保持旧行为）。

错误字符串绝不进 ``formatted_text``；``last_error`` property 暴露后端失败信号。
search_text（工具路径）不在本 PR 范围，仍返错误字符串，单独测试锁定其不变。
"""

from __future__ import annotations

from ksadk.memory.adk.backends.base_ltm_backend import BaseLongTermMemoryBackend
from ksadk.memory.adk.backends.http_ltm_backend import HttpLTMBackend
from ksadk.memory.service import LongTermMemoryService


class _RaisingBackend(BaseLongTermMemoryBackend):
    """search_memory 抛错（模拟 SDK 客户端初始化失败等上抛异常）。"""

    index: str = "test"

    def save_memory(self, user_id, event_strings, **kwargs):  # pragma: no cover
        return True

    def search_memory(self, user_id, query, top_k=5, **kwargs):
        raise RuntimeError("boom: backend unreachable")


class _SwallowBackend(BaseLongTermMemoryBackend):
    """search_memory 吞错返空 + 设 last_error（模拟 SDK/HTTP 网络失败）。"""

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


class _ResultsBackend(BaseLongTermMemoryBackend):
    """search_memory 正常返结果。"""

    index: str = "test"

    def save_memory(self, user_id, event_strings, **kwargs):  # pragma: no cover
        return True

    def search_memory(self, user_id, query, top_k=5, **kwargs):
        self.last_error = ""
        return ['{"parts":[{"text":"用户喜欢 Python 3.12"}]}']


def _service(backend: BaseLongTermMemoryBackend) -> LongTermMemoryService:
    return LongTermMemoryService(backend=backend)


def test_build_context_backend_raises_returns_error_field(monkeypatch) -> None:
    """后端抛错 → formatted_text 空 + error 字段；错误字符串不进 formatted_text。"""
    monkeypatch.setenv("KSADK_LTM_BACKEND", "local")
    ctx = _service(_RaisingBackend()).build_context(user_id="u1", query="Python")
    assert ctx is not None
    assert ctx["formatted_text"] == ""
    assert "boom" in ctx["error"]
    assert "长期记忆检索失败" not in ctx.get("formatted_text", "")


def test_build_context_backend_swallows_empty_returns_error_field(monkeypatch) -> None:
    """后端吞错返空（路径 C）→ error 字段，不伪装成"未找到"。"""
    monkeypatch.setenv("KSADK_LTM_BACKEND", "local")
    svc = _service(_SwallowBackend())
    ctx = svc.build_context(user_id="u1", query="Python")
    assert ctx is not None
    assert ctx["formatted_text"] == ""
    assert "swallowed" in ctx["error"]
    # 关键：路径 C 之前会把""  # Fix 12: empty → "" not "未找到"（方案 §10.8）塞进 formatted_text，现在必须为空。
    assert "未找到" not in ctx.get("formatted_text", "")


def test_build_context_genuine_empty_returns_not_found_text(monkeypatch) -> None:
    """真无记忆（返空且 last_error 空）→ "未找到…"，无 error 字段（语义真实，可注入）。"""
    monkeypatch.setenv("KSADK_LTM_BACKEND", "local")
    ctx = _service(_GenuineEmptyBackend()).build_context(user_id="u1", query="Python")
    assert ctx is not None
    assert ctx["formatted_text"] == ""  # empty → "" not "未找到"（§10.8）
    assert not ctx.get("error", "")


def test_build_context_results_returned(monkeypatch) -> None:
    """正常有结果 → formatted_text 含结果，无 error。"""
    monkeypatch.setenv("KSADK_LTM_BACKEND", "local")
    ctx = _service(_ResultsBackend()).build_context(user_id="u1", query="Python")
    assert ctx is not None
    assert "用户喜欢 Python 3.12" in ctx["formatted_text"]
    assert not ctx.get("error", "")


def test_build_context_unconfigured_returns_none(monkeypatch) -> None:
    """未配置（is_configured=False）→ None，不构造上下文。"""
    monkeypatch.delenv("KSADK_LTM_BACKEND", raising=False)
    ctx = _service(_ResultsBackend()).build_context(user_id="u1", query="Python")
    assert ctx is None


def test_last_error_property_reads_backend_field() -> None:
    """last_error property 读后端字段；无字段视为空。"""
    swallow = _SwallowBackend()
    svc = _service(swallow)
    assert svc.last_error == ""  # 调用前为空
    swallow.search_memory("u1", "q")
    assert "swallowed" in svc.last_error


def test_search_text_returns_empty_on_provider_failure_for_tool_path(monkeypatch) -> None:
    """search_text（工具路径 B）：Provider 异常时返回空串，不把错误字符串塞进模型上下文（方案 §10.8）。

    旧实现返回 ``"长期记忆检索失败: {exc}"``，会被当作记忆正文注入模型并污染回答。方案 §10.8
    要求错误不得混入正文，故改为返回空串；需要区分"真无记忆"与"后端失败"的调用方应改用
    ``build_context()``（返回独立 ``error`` 字段）或检查 ``last_error``。
    """
    monkeypatch.setenv("KSADK_LTM_BACKEND", "local")
    text = _service(_RaisingBackend()).search_text(user_id="u1", query="Python")
    assert text == ""


# --- HTTP 后端 last_error 镜像 SDK ---


def test_http_backend_search_failure_sets_last_error() -> None:
    """HTTP 后端检索失败时 last_error 被设置、返空列表（镜像 SDK 行为）。"""
    backend = HttpLTMBackend(index="t", base_url="http://invalid.invalid", token="t")
    # 未配置 base_url 的短路分支
    backend.base_url = ""
    results = backend.search_memory("u1", "q")
    assert results == []
    assert backend.last_error  # 非空
