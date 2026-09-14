"""Provider 模型能力声明与运行时流式模式自动选择。"""

from __future__ import annotations

import pytest

from ksadk.harness.model_capability import (
    ModelCapabilityDeclaration,
    ModelCapabilityStore,
    apply_matrix_report,
    capabilities_from_matrix_report,
    global_capability_store,
    load_capability_store,
    reset_global_capability_store,
    resolve_streaming_mode,
)
from ksadk.harness.reasoner import LiteLLMHarnessReasoner


def _matrix_report(stream_tool: bool, stream_text: bool = True) -> dict:
    return {
        "endpoint": "http://gateway.test/v1",
        "results": [
            {
                "model_id": "qwen3-8-max",
                "basic_chat": True,
                "tool_calling": True,
                "usage_reported": True,
                "stream_text": stream_text,
                "stream_tool_calling": stream_tool,
            }
        ],
    }


def test_matrix_report_converts_to_minimal_declarations() -> None:
    declarations = capabilities_from_matrix_report(_matrix_report(stream_tool=False))
    assert len(declarations) == 1
    item = declarations[0]
    assert item.model_id == "qwen3-8-max"
    assert item.supports_basic_chat is True
    assert item.supports_tool_calling is True
    assert item.supports_streaming is True
    assert item.supports_streaming_tool_calls is False
    assert "gateway.test" in item.source


def test_unknown_matrix_dimensions_stay_none() -> None:
    declarations = capabilities_from_matrix_report(
        {"endpoint": "e", "results": [{"model_id": "m", "basic_chat": True}]}
    )
    assert declarations[0].supports_streaming_tool_calls is None


def test_resolve_streaming_mode_degrades_only_on_declared_failure() -> None:
    store = ModelCapabilityStore()
    # 未声明：不猜测，保持请求值。
    assert resolve_streaming_mode(requested=True, model_id="m", store=store) is True
    store.declare(
        ModelCapabilityDeclaration(
            model_id="qwen3-8-max", supports_streaming_tool_calls=False
        )
    )
    # 已声明不支持流式 Tool Call：降级。
    assert resolve_streaming_mode(requested=True, model_id="qwen3-8-max", store=store) is False
    # provider 前缀别名同样命中（resolved model id 形如 openai/qwen3-8-max）。
    assert (
        resolve_streaming_mode(requested=True, model_id="openai/qwen3-8-max", store=store)
        is False
    )
    # 非流式请求永不升级。
    assert resolve_streaming_mode(requested=False, model_id="qwen3-8-max", store=store) is False
    # 其他模型不受影响。
    assert resolve_streaming_mode(requested=True, model_id="kimi-k3", store=store) is True


def test_streaming_only_failure_still_degrades() -> None:
    store = ModelCapabilityStore()
    store.declare(ModelCapabilityDeclaration(model_id="m", supports_streaming=False))
    assert resolve_streaming_mode(requested=True, model_id="m", store=store) is False
    # 声明支持 → 保持请求值。
    store.declare(
        ModelCapabilityDeclaration(
            model_id="m2", supports_streaming=True, supports_streaming_tool_calls=True
        )
    )
    assert resolve_streaming_mode(requested=True, model_id="m2", store=store) is True


def test_store_roundtrip_via_file(tmp_path) -> None:
    store = ModelCapabilityStore()
    apply_matrix_report(_matrix_report(stream_tool=False), store)
    path = tmp_path / "capabilities.json"
    store.save(path)
    loaded = load_capability_store(path)
    assert loaded is not None
    declaration = loaded.lookup("qwen3-8-max")
    assert declaration is not None
    assert declaration.supports_streaming_tool_calls is False


def test_missing_capability_file_returns_none(tmp_path) -> None:
    assert load_capability_store(tmp_path / "nope.json") is None


def test_global_store_loads_from_env_file(monkeypatch, tmp_path) -> None:
    reset_global_capability_store()
    store = ModelCapabilityStore()
    apply_matrix_report(_matrix_report(stream_tool=False), store)
    path = tmp_path / "capabilities.json"
    store.save(path)
    monkeypatch.setenv("KSADK_MODEL_CAPABILITY_FILE", str(path))
    try:
        loaded = global_capability_store()
        assert loaded is not None
        assert loaded.lookup("qwen3-8-max") is not None
    finally:
        reset_global_capability_store()


class _FakeAsyncClient:
    """记录 acompletion 请求是否带 stream，返回非流式形状响应。"""

    calls: list[dict] = []

    def __init__(self, *args, **kwargs):
        self._kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post_completion(self):  # pragma: no cover - helper shape
        raise NotImplementedError


def _install_litellm_stub(monkeypatch, responses: list[dict]) -> list[dict]:
    calls: list[dict] = []

    class _Response:
        def __init__(self, payload):
            self.choices = [type("C", (), {"message": type("M", (), payload)})()]
            self.usage = None

    async def fake_acompletion(**kwargs):
        calls.append(kwargs)
        return _Response(responses[min(len(calls) - 1, len(responses) - 1)])

    import sys

    litellm_stub = type(sys)("litellm")
    litellm_stub.acompletion = fake_acompletion
    monkeypatch.setitem(sys.modules, "litellm", litellm_stub)
    return calls


@pytest.mark.asyncio
async def test_reasoner_degrades_to_non_streaming_for_declared_model(monkeypatch):
    calls = _install_litellm_stub(
        monkeypatch,
        [{"content": "ok", "tool_calls": None}],
    )
    store = ModelCapabilityStore()
    apply_matrix_report(_matrix_report(stream_tool=False), store)
    reasoner = LiteLLMHarnessReasoner(streaming=True, capability_store=store)

    turn = await reasoner.complete(
        model="qwen3-8-max",
        prompt="",
        messages=({"role": "user", "content": "hi"},),
        tools=(),
    )

    assert turn.final_text == "ok"
    assert calls[0].get("stream") is None or calls[0].get("stream") is False
    assert reasoner.last_streaming_mode is False


@pytest.mark.asyncio
async def test_reasoner_keeps_streaming_for_supported_model(monkeypatch):
    class _StreamChunk:
        def __init__(self, content):
            self.usage = None
            self.choices = [
                type(
                    "C",
                    (),
 {
                        "delta": type("D", (), {"content": content, "tool_calls": None})(),
                        "finish_reason": "stop",
                    },
                )()
            ]

    class _Stream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if not getattr(self, "_done", False):
                self._done = True
                return _StreamChunk("STREAM_OK")
            raise StopAsyncIteration

    calls: list[dict] = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs)
        return _Stream()

    import sys

    litellm_stub = type(sys)("litellm")
    litellm_stub.acompletion = fake_acompletion
    monkeypatch.setitem(sys.modules, "litellm", litellm_stub)

    store = ModelCapabilityStore()
    store.declare(
        ModelCapabilityDeclaration(
            model_id="kimi-k3",
            supports_streaming=True,
            supports_streaming_tool_calls=True,
        )
    )
    reasoner = LiteLLMHarnessReasoner(streaming=True, capability_store=store)
    turn = await reasoner.complete(
        model="kimi-k3",
        prompt="",
        messages=({"role": "user", "content": "hi"},),
        tools=(),
    )
    assert turn.final_text == "STREAM_OK"
    assert calls[0]["stream"] is True
    assert reasoner.last_streaming_mode is True




def test_default_mode_upgrades_to_streaming_when_non_stream_tool_calls_broken() -> None:
    # 实测形态（qwen3-8-max）：非流式不发起 Tool Call，流式正常。
    store = ModelCapabilityStore()
    store.declare(
        ModelCapabilityDeclaration(
            model_id="qwen3-8-max",
            supports_tool_calling=False,
            supports_streaming=True,
            supports_streaming_tool_calls=True,
        )
    )
    # 默认非流式 + 允许升级 → 自动选择流式。
    assert (
        resolve_streaming_mode(
            requested=False, model_id="qwen3-8-max", store=store, allow_upgrade=True
        )
        is True
    )
    # 不允许升级（显式关闭流式）→ 保持非流式。
    assert (
        resolve_streaming_mode(
            requested=False, model_id="qwen3-8-max", store=store, allow_upgrade=False
        )
        is False
    )
    # 非流式 Tool Call 正常的模型不升级。
    store.declare(ModelCapabilityDeclaration(model_id="kimi-k3", supports_tool_calling=True))
    assert (
        resolve_streaming_mode(
            requested=False, model_id="kimi-k3", store=store, allow_upgrade=True
        )
        is False
    )


def test_unknown_model_keeps_requested_mode_with_allow_upgrade() -> None:
    store = ModelCapabilityStore()
    assert (
        resolve_streaming_mode(
            requested=False, model_id="unknown", store=store, allow_upgrade=True
        )
        is False
    )
