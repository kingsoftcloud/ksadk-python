"""收口 6：Runner Conformance Matrix（plan §15.2）。

同一套 Conformance 套件参数化跑在所有已注册 Runner 上：managed-langgraph
（KsADK 默认 Harness 引擎）、native-harness-adapter（HarnessRuntimeAdapter）、
adk、external-langgraph、codex（外部 Runner 经各自 RuntimeAdapter 驱动）。
另有守恒测试：矩阵必须显式覆盖 factory 注册的全部 Runner，不允许静默漏测。
"""

from __future__ import annotations

import asyncio

import pytest

from ksadk.harness.conformance import run_conformance_suite
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.reasoner import HarnessReasoner, HarnessReasoningTurn
from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
from ksadk.runtime import StartRequest


class _NoToolReasoner(HarnessReasoner):
    async def complete(self, *, model, prompt, messages, tools):
        del model, prompt, messages, tools
        return HarnessReasoningTurn(
            final_text="好的", usage={"input_tokens": 10, "output_tokens": 4}
        )


def _managed_langgraph_stream() -> list:
    async def drive() -> list:
        engine = ManagedLangGraphEngine(reasoner=_NoToolReasoner())
        spec = HarnessSpec(
            agent_revision_ref="agent-revision://proj-1@1",
            model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
            prompt=PromptSpec(instructions="助手"),
        )
        compiled = await engine.compile(spec)
        handle = await engine.start(
            StartRequest(
                input="你好",
                user_id="u",
                session_id="s",
                agent_id="a",
                runtime_type="managed-langgraph",
                metadata={"invocation_id": "run-conf"},
            ),
            compiled,
        )
        return [e async for e in engine.stream(handle)]

    return asyncio.run(drive())


def _native_harness_adapter_stream() -> list:
    """Studio 默认链路背后的 HarnessRuntimeAdapter。"""
    from ksadk.harness.config import HarnessConfig
    from ksadk.harness.runtime import HarnessRuntimeAdapter

    adapter = HarnessRuntimeAdapter(
        HarnessConfig(model="m", prompt="p"),
        reasoner=_NoToolReasoner(),
        workspace_root="/tmp",
    )
    request = StartRequest(
        agent_id="a",
        user_id="u",
        session_id="s",
        input="hi",
        runtime_type="harness",
    )

    async def collect() -> list:
        handle = await adapter.start(request)
        return [event async for event in adapter.stream(handle)]

    return asyncio.run(collect())


def _external_adapter_stream(make_adapter):  # type: ignore[no-untyped-def]
    """经 RunnerRuntimeAdapter（adk/langgraph）或 CodexRuntimeAdapter 驱动。

    无审批场景（with_approval=False）：审批挂起是合法的运行中挂起，不属于
    终止语义，矩阵用无审批流校验生命周期/配对/脱敏契约。
    """
    from ksadk.runtime import StartRequest

    async def drive() -> list:
        adapter, _runner = make_adapter(block=False, with_approval=False)
        handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="s"))
        return [event async for event in adapter.stream(handle)]

    return asyncio.run(drive())


def _adk_stream() -> list:
    from tests.runners.test_adapter_contract import _make_adk

    return _external_adapter_stream(_make_adk)


def _external_langgraph_stream() -> list:
    from tests.runners.test_adapter_contract import _make_langgraph

    return _external_adapter_stream(_make_langgraph)


def _codex_stream() -> list:
    from tests.runners.test_adapter_contract import _make_codex

    return _external_adapter_stream(_make_codex)


#: 已接入数据面（工厂可跑真实事件流）的 Runner。
_IMPLEMENTED = {
    "managed-langgraph": _managed_langgraph_stream,
    "native-harness-adapter": _native_harness_adapter_stream,
    "adk": _adk_stream,
    "external-langgraph": _external_langgraph_stream,
    "codex": _codex_stream,
}


def _matrix_cases():
    return [pytest.param(name, factory, id=name) for name, factory in _IMPLEMENTED.items()]


@pytest.mark.parametrize(("runner", "factory"), _matrix_cases())
def test_runner_conformance_matrix(runner, factory):
    """矩阵中的每个 Runner：相同外部契约、事件语义一致、能力诚实声明。"""
    events = factory()
    assert events, f"{runner} 未产出事件流"
    report = run_conformance_suite(events, cancel_requested=False)
    assert report.ok, [(v.rule, v.detail) for v in report.violations]


def test_matrix_covers_all_registered_runners():
    """矩阵必须显式列出所有已知 Runner（不允许静默漏测）。"""
    from ksadk.runtime.factory import build_default_runtime_registry

    known = set(_IMPLEMENTED)
    # factory 注册的 runner 名归一（managed ↔ managed-langgraph；
    # langgraph ↔ external-langgraph）。
    normalize = {
        "managed": "managed-langgraph",
        "langgraph": "external-langgraph",
    }
    registered = {
        normalize.get(name, name) for name in build_default_runtime_registry().registered_types()
    }
    missing = registered - known
    assert not missing, f"矩阵漏测已注册 Runner: {missing}"
