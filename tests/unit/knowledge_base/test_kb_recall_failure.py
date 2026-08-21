"""PR（Memory Recall 失败语义）：知识库环境上下文不得把 Provider 异常/响应解析失败
伪装成"未找到"正文注入模型。

覆盖 KnowledgeBaseService.build_context 的三条分支：
- 检索抛错（网络/鉴权失败）→ ``formatted_text=""`` + 独立 ``error`` 字段。
- 响应解析失败返空（``_parse_response`` 设 client ``last_error``）→ ``error`` 字段（堵路径 C）。
- 真无结果（检索正常返空）→ ``formatted_text="未找到相关知识库内容。"``
  （语义真实，可注入；非失败，保持旧行为）。

错误字符串绝不进 ``formatted_text``。
"""

from __future__ import annotations

from ksadk.knowledge_base.client import KnowledgeBaseResult
from ksadk.knowledge_base.service import KnowledgeBaseService


class _FakeClient:
    """鸭子类型 KB client：可控 search 行为 + last_error 信号。"""

    def __init__(self, *, mode: str, payload: str = ""):
        self._mode = mode
        self._payload = payload
        self.last_error = ""

    def search(self, query, top_k=None):
        if self._mode == "raise":
            self.last_error = "boom: kb unreachable"
            raise RuntimeError(self._payload or "boom: kb unreachable")
        if self._mode == "parse_fail":
            # 模拟 _parse_response 解析失败：返空 + 设 last_error
            self.last_error = "Failed to parse response: <garbage>"
            return []
        # genuine empty
        self.last_error = ""
        return []

    def _result(self) -> list[KnowledgeBaseResult]:  # pragma: no cover - helper
        return [KnowledgeBaseResult(content="KB 内容 A", document_name="doc.md")]


class _ResultsClient(_FakeClient):
    def __init__(self):
        super().__init__(mode="", payload="")

    def search(self, query, top_k=None):
        self.last_error = ""
        return [KnowledgeBaseResult(content="KB 内容 A", document_name="doc.md")]


def _service(client: _FakeClient) -> KnowledgeBaseService:
    return KnowledgeBaseService(client=client)


def test_build_context_search_raises_returns_error_field(monkeypatch) -> None:
    monkeypatch.setenv("KSADK_KB_DATASET_ID", "ds")
    ctx = _service(_FakeClient(mode="raise")).build_context("KCE")
    assert ctx is not None
    assert ctx["formatted_text"] == ""
    assert "boom" in ctx["error"]
    assert "知识库检索失败" not in ctx.get("formatted_text", "")


def test_build_context_parse_failure_returns_error_field(monkeypatch) -> None:
    """响应解析失败返空（路径 C）→ error 字段，不伪装成"未找到"。"""
    monkeypatch.setenv("KSADK_KB_DATASET_ID", "ds")
    ctx = _service(_FakeClient(mode="parse_fail")).build_context("KCE")
    assert ctx is not None
    assert ctx["formatted_text"] == ""
    assert "parse" in ctx["error"]
    assert "未找到" not in ctx.get("formatted_text", "")


def test_build_context_genuine_empty_returns_not_found_text(monkeypatch) -> None:
    """真无结果（返空且 last_error 空）→ "未找到…"，无 error 字段（可注入）。"""
    monkeypatch.setenv("KSADK_KB_DATASET_ID", "ds")
    ctx = _service(_FakeClient(mode="empty")).build_context("KCE")
    assert ctx is not None
    assert ctx["formatted_text"] == "未找到相关知识库内容。"
    assert not ctx.get("error", "")


def test_build_context_results_returned(monkeypatch) -> None:
    monkeypatch.setenv("KSADK_KB_DATASET_ID", "ds")
    ctx = _service(_ResultsClient()).build_context("KCE")
    assert ctx is not None
    assert "KB 内容 A" in ctx["formatted_text"]
    assert "doc.md" in ctx["formatted_text"]
    assert not ctx.get("error", "")


def test_build_context_unconfigured_returns_none(monkeypatch) -> None:
    monkeypatch.delenv("KSADK_KB_DATASET_ID", raising=False)
    ctx = _service(_ResultsClient()).build_context("KCE")
    assert ctx is None


def test_service_last_error_reads_client_field(monkeypatch) -> None:
    monkeypatch.setenv("KSADK_KB_DATASET_ID", "ds")
    client = _FakeClient(mode="raise")
    svc = _service(client)
    assert svc.last_error == ""
    try:
        svc.search("KCE")
    except RuntimeError:
        pass
    assert "boom" in svc.last_error
