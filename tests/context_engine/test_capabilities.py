from __future__ import annotations

from types import SimpleNamespace

from ksadk.context_engine.capabilities import (
    DEFAULT_CONTEXT_CAPABILITIES,
    ContextCapabilities,
    adk_context_capabilities,
    capabilities_for_runner,
    capabilities_for_runtime_type,
    capability_hash,
    codex_context_capabilities,
    deepagents_context_capabilities,
    langchain_context_capabilities,
    langgraph_context_capabilities,
)


def test_default_capabilities_are_conservative_and_opaque() -> None:
    caps = DEFAULT_CONTEXT_CAPABILITIES()
    assert caps.integration_mode == "framework_assisted"
    assert caps.token_accounting == "opaque"
    assert caps.prompt_projection == frozenset()
    for owner in (
        caps.prompt_owner,
        caps.history_owner,
        caps.compaction_owner,
        caps.memory_owner,
        caps.skill_owner,
    ):
        assert owner == "framework"
    assert not caps.memory_read
    assert not caps.memory_write
    assert not caps.core_memory
    assert not caps.native_skills
    assert not caps.supports_context_snapshot


def test_default_capabilities_returns_fresh_frozen_instance() -> None:
    # 返回新实例，避免共享可变默认；frozen 保证不可变。
    assert DEFAULT_CONTEXT_CAPABILITIES() == DEFAULT_CONTEXT_CAPABILITIES()
    assert isinstance(DEFAULT_CONTEXT_CAPABILITIES(), ContextCapabilities)


def test_adk_capabilities_match_real_ownership() -> None:
    caps = adk_context_capabilities()
    assert caps.integration_mode == "framework_assisted"
    assert caps.history_owner == "framework"  # ADK SessionService 拥有，忽略 payload.history
    assert caps.memory_owner == "framework"
    assert caps.memory_read and caps.memory_write
    assert caps.native_skills is True
    assert caps.token_accounting == "runtime_reported"
    assert "instruction" in caps.prompt_projection


def test_langgraph_capabilities_match_real_ownership() -> None:
    caps = langgraph_context_capabilities()
    assert caps.integration_mode == "framework_assisted"
    assert caps.prompt_owner == "ksadk"
    assert caps.history_owner == "ksadk"  # runner 组装 history
    assert caps.compaction_owner == "ksadk"
    assert caps.memory_owner == "framework"
    assert caps.memory_read is True and caps.memory_write is False
    assert caps.native_skills is False
    assert caps.token_accounting == "estimated"
    assert "system_message" in caps.prompt_projection


def test_langchain_and_deepagents_inherit_langgraph_projection_but_hand_off_history() -> None:
    for caps_factory in (langchain_context_capabilities, deepagents_context_capabilities):
        caps = caps_factory()
        assert caps.integration_mode == "framework_assisted"
        assert caps.prompt_owner == "ksadk"
        assert caps.history_owner == "framework"
        assert caps.compaction_owner == "framework"
        assert caps.token_accounting == "estimated"
        assert caps.supports_context_snapshot is False


def test_codex_capabilities_are_native_runtime() -> None:
    caps = codex_context_capabilities()
    assert caps.integration_mode == "native_runtime"
    for owner in (
        caps.prompt_owner,
        caps.history_owner,
        caps.compaction_owner,
        caps.memory_owner,
        caps.skill_owner,
    ):
        assert owner == "native"
    assert not caps.memory_read and not caps.memory_write
    assert caps.token_accounting == "runtime_reported"
    assert "base_instructions" in caps.prompt_projection


def _runner_with_type(value: str) -> SimpleNamespace:
    return SimpleNamespace(detection_result=SimpleNamespace(type=SimpleNamespace(value=value)))


def test_capabilities_for_runner_dispatches_known_types() -> None:
    cases = {
        "adk": adk_context_capabilities(),
        "langgraph": langgraph_context_capabilities(),
        "langchain": langchain_context_capabilities(),
        "deepagents": deepagents_context_capabilities(),
        "codex": codex_context_capabilities(),
        "ADK": adk_context_capabilities(),  # 大小写不敏感
    }
    for value, expected in cases.items():
        caps = capabilities_for_runner(_runner_with_type(value))
        assert caps.integration_mode == expected.integration_mode
        assert caps.token_accounting == expected.token_accounting


def test_capabilities_for_runner_unknown_falls_back_to_default_without_hasattr_guess() -> None:
    # 未知 detection type + 无 describe_context_capabilities → DEFAULT，不靠 hasattr 推断 ownership。
    unknown = _runner_with_type("weird-custom-framework")
    caps = capabilities_for_runner(unknown)
    assert caps.integration_mode == "framework_assisted"
    assert caps.token_accounting == "opaque"

    # detection_result 缺失也走 DEFAULT。
    caps_missing = capabilities_for_runner(SimpleNamespace())
    assert caps_missing.token_accounting == "opaque"

    # None runner 走 DEFAULT。
    assert capabilities_for_runner(None).token_accounting == "opaque"


def test_capabilities_for_runner_prefers_explicit_describe_method() -> None:
    # Runner 显式声明优先于 detection type 分派（自定义 Runner 可声明真实 ownership）。
    explicit = codex_context_capabilities()

    class _ExplicitRunner:
        def describe_context_capabilities(self):
            return explicit

    assert capabilities_for_runner(_ExplicitRunner()) is explicit

    class _BrokenDescribeRunner:
        def describe_context_capabilities(self):
            raise RuntimeError("boom")

    # describe 抛异常时回退到 detection type 分派 / DEFAULT，不把异常透出。
    caps = capabilities_for_runner(_BrokenDescribeRunner())
    assert caps.token_accounting == "opaque"


def test_capabilities_for_runtime_type_dispatches_known_types() -> None:
    """canonical conversation execution 路径用 runtime_type 取 capability，不再 opaque。"""
    assert capabilities_for_runtime_type("adk") == adk_context_capabilities()
    assert capabilities_for_runtime_type("langgraph") == langgraph_context_capabilities()
    assert capabilities_for_runtime_type("codex") == codex_context_capabilities()
    # 大小写/空白不敏感
    assert capabilities_for_runtime_type("  LangGraph  ") == langgraph_context_capabilities()
    # 未知 → DEFAULT
    assert capabilities_for_runtime_type("weird") == DEFAULT_CONTEXT_CAPABILITIES()
    assert capabilities_for_runtime_type(None) == DEFAULT_CONTEXT_CAPABILITIES()
    assert capabilities_for_runtime_type("") == DEFAULT_CONTEXT_CAPABILITIES()


def test_capability_hash_is_stable_and_distinguishes_ownerships() -> None:
    """capability_hash 对相同 capability 稳定，对不同 ownership 不同。"""
    a = capability_hash(langgraph_context_capabilities())
    b = capability_hash(langgraph_context_capabilities())
    assert a == b
    assert a.startswith("sha256:")

    codex_h = capability_hash(codex_context_capabilities())
    default_h = capability_hash(DEFAULT_CONTEXT_CAPABILITIES())
    assert a != codex_h != default_h


def test_capability_hash_ignores_prompt_projection_order() -> None:
    """prompt_projection 是 frozenset，hash 不受元素顺序影响。"""
    from dataclasses import replace

    caps = adk_context_capabilities()
    reordered = replace(caps, prompt_projection=frozenset(reversed(sorted(caps.prompt_projection))))
    assert capability_hash(caps) == capability_hash(reordered)
